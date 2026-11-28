"""RELEASING.md says the release check refuses three disagreeing spellings.

Until this file existed, nothing did. `git grep -n "__version__"` over
`floati/`, `scripts/`, `tests/` and `.github/` returned two consumers of the
value and zero readers of the changelog or the tag: the sentence in
RELEASING.md — "the tag, the changelog heading, and `__version__` must be
three spellings of the same value; the release check refuses otherwise" —
was a promise asserted by a document and enforced by nobody.

The last test here is the one that matters after today: it runs the check
against this repository, so the three spellings cannot drift apart again
without the suite going red.

RELEASE-CHECK-PYPROJECT-1 names a fourth spelling the check never reads:
`pyproject.toml` carries `[project] version`, the value the packaging tools
read. One deviation must refuse naming every spelling, two must both be
counted, and zero must pass unchanged.
"""

from __future__ import annotations

import unittest
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from unittest import mock

from tests.ledger_contract_candidate import copy_live_contract_inputs
from tests.temp_roots import REAL_TEMP_ROOT

from floati.release import (
    check_release,
    changelog_heading,
    module_version,
    pyproject_version,
    readme_claim_census,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ReleaseSpellingCheckTests(unittest.TestCase):
    """Three spellings of one value, and a typed refusal for every way to disagree."""

    @classmethod
    def setUpClass(cls):
        from scripts import ledger_contract

        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.contract_template = Path(temporary.name)
        root = cls.contract_template
        (root / "floati").mkdir()
        (root / "floati/records.py").write_text(
            'def validate(row):\n    return row["kind"] == "sample"\n'
        )
        (root / "schemas/v0").mkdir(parents=True)
        (root / "schemas/v0/sample.schema.json").write_text(json.dumps({
            "type": "object", "required": ["kind"],
            "properties": {"kind": {"const": "sample"}},
        }) + "\n")
        (root / "bundle/c7.2").mkdir(parents=True)
        ledger_contract.cut(root, "c7.3", ledger_contract.inventory(root))

    def _tree(
        self, *, version: str, heading: str, pyproject: Optional[str] = None
    ) -> Path:
        import tempfile

        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root, True)
        shutil.copytree(self.contract_template, root, dirs_exist_ok=True)
        (root / "floati" / "__init__.py").write_text(
            '"""Floati protocol core."""\n\n__version__ = "%s"\n' % version,
            encoding="utf-8",
        )
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n%s\n\nLedger contract: `c7.3`.\n\nprose.\n" % heading,
            encoding="utf-8",
        )
        (root / "pyproject.toml").write_text(
            '[project]\nname = "floati"\nversion = "%s"\n'
            % (version if pyproject is None else pyproject),
            encoding="utf-8",
        )
        return root

    def test_preparation_without_a_cut_cannot_authorize_release(self):
        """Prepared pins cover drift, but they are not a published contract."""
        from scripts import ledger_contract

        root = self._tree(version="0.1.0", heading="## [Unreleased]")
        shutil.rmtree(root / "bundle/c7.3")
        (root / ledger_contract.CURRENT).unlink()
        (root / "floati/records.py").write_text(
            'def validate(row):\n    return row["kind"] == "prepared_only"\n'
        )
        ledger_contract.write_json(root / ledger_contract.PREPARATION, {
            "state": "prepared_not_cut", "bundles": ["c7.2"],
            "members": ledger_contract.inventory(root),
        })
        self.assertEqual("prepared", ledger_contract.check(root)["classification"])
        self.assertIn("ledger_contract_cut_required", check_release(root))

    def test_next_release_declaration_leaves_shipped_notes_unchanged(self):
        root = self._tree(version="0.1.0", heading="## [0.1.0] — 2026-08-31")
        changelog = root / "CHANGELOG.md"
        shipped = changelog.read_text().replace("Ledger contract: `c7.3`.\n\n", "")
        changelog.write_text(shipped.replace(
            "# Changelog\n\n",
            "# Changelog\n\n## [Unreleased]\n\nLedger contract: `c7.3`.\n\n",
        ))
        self.assertEqual([], check_release(root))
        self.assertEqual(shipped.split("## [0.1.0]", 1)[1],
                         changelog.read_text().split("## [0.1.0]", 1)[1])
        changelog.write_text(changelog.read_text().replace("`c7.3`", "`c7.2`"))
        self.assertIn("ledger_contract_version_mismatch:notes=c7.2,latest=c7.3",
                      check_release(root))

    def test_existing_tag_uses_its_own_tree_and_reports_historical_absence(self):
        from floati.release import main
        from contextlib import redirect_stderr
        from io import StringIO
        from unittest.mock import patch

        for declaration, error in (
            (None, None), ("c7.3", None),
            ("c7.2", "ledger_contract_version_mismatch:notes=c7.2,latest=c7.3"),
            ("c7.03", "ledger_contract_notes_invalid"),
            ("duplicate", "ledger_contract_notes_invalid"),
            ("absent_with_drift", "ledger_contract_recut_required:"),
            ("noncommit", "tag_tree_unreadable:v0.1.0"),
        ):
            with self.subTest(declaration=declaration):
                root = self._tree(version="0.1.0", heading="## [0.1.0] — 2026-08-31")
                changelog = root / "CHANGELOG.md"
                if declaration is None:
                    changelog.write_text(changelog.read_text().replace(
                        "Ledger contract: `c7.3`.\n\n", ""))
                    shutil.rmtree(root / "bundle")
                elif declaration == "duplicate":
                    changelog.write_text(changelog.read_text() + "\nLedger contract: `c7.3`.\n")
                elif declaration == "absent_with_drift":
                    changelog.write_text(changelog.read_text().replace(
                        "Ledger contract: `c7.3`.\n\n", ""))
                    (root / "floati/records.py").write_text("def validate(row):\n    return False\n")
                elif declaration == "noncommit":
                    pass
                else:
                    changelog.write_text(changelog.read_text().replace("c7.3", declaration))
                # Export attributes must not remove or rewrite a tagged record.
                (root / ".gitattributes").write_text("CHANGELOG.md export-ignore\n")
                for arguments in (
                    ["init", "-q"], ["add", "-A"],
                    ["-c", "user.name=Release fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "-qm", "Record release fixture."],
                    ["tag", "v0.1.0"],
                ):
                    subprocess.run(["/usr/bin/git", "-C", str(root), *arguments],
                                   check=True, capture_output=True)
                if declaration == "noncommit":
                    blob = subprocess.run(
                        ["/usr/bin/git", "-C", str(root), "rev-parse", "HEAD:CHANGELOG.md"],
                        check=True, capture_output=True, text=True,
                    ).stdout.strip()
                    subprocess.run(["/usr/bin/git", "-C", str(root), "tag", "-f", "v0.1.0", blob],
                                   check=True, capture_output=True)
                # Neither uncommitted notes nor a newer working cut describe this tag.
                changelog.write_text("# Changelog\n\n## [Unreleased]\n\nLedger contract: `c7.99`.\n")
                (root / "floati/records.py").write_text("changed after release\n")
                stderr = StringIO()
                with redirect_stderr(stderr), patch.dict("os.environ", {"GIT_DIR": str(root / "foreign.git")}):
                    result = main(["--repository-root", str(root), "--tag", "v0.1.0"])
                self.assertEqual(1 if error else 0, result, stderr.getvalue())
                if declaration is None:
                    self.assertIn("ledger_contract_declaration_absent:v0.1.0", stderr.getvalue())
                elif error:
                    self.assertIn(error, stderr.getvalue())

    def test_release_notes_must_name_the_latest_cut(self):
        root = self._tree(version="0.1.0", heading="## [Unreleased]")
        changelog = root / "CHANGELOG.md"
        changelog.write_text(changelog.read_text().replace("`c7.3`", "`c7.2`"))
        self.assertIn(
            "ledger_contract_version_mismatch:notes=c7.2,latest=c7.3",
            check_release(root),
        )

    def test_release_refuses_schema_and_runtime_drift_from_the_cut(self):
        for relative, replacement in (
            ("schemas/v0/sample.schema.json", '{}\n'),
            ("floati/records.py", 'def validate(row):\n    return False\n'),
        ):
            with self.subTest(relative=relative):
                root = self._tree(version="0.1.0", heading="## [Unreleased]")
                (root / relative).write_text(replacement)
                self.assertTrue(any(
                    error.startswith("ledger_contract_recut_required:")
                    for error in check_release(root)
                ))

    def test_contract_notes_are_required_once_in_the_current_entry(self):
        root = self._tree(version="0.1.0", heading="## [Unreleased]")
        changelog = root / "CHANGELOG.md"
        current = changelog.read_text()
        for source in (
            current.replace("Ledger contract: `c7.3`.\n", ""),
            current + "\nLedger contract: `c7.3`.\n",
            current.replace("Ledger contract: `c7.3`.\n", "")
            + "\n## [0.0.9] — 2026-01-01\n\nLedger contract: `c7.3`.\n",
            current.replace("`c7.3`.", "`c7.03`."),
            current.replace("`c7.3`.", "`c7.3`"),
            current.replace("`c7.3`.", "`c7.3`. Extra prose."),
        ):
            with self.subTest(source=source):
                changelog.write_text(source)
                self.assertIn("ledger_contract_notes_invalid", check_release(root))

    def test_release_accepts_a_fixture_ratified_schema_loosening(self):
        from scripts import ledger_contract

        root = self._tree(version="0.1.0", heading="## [Unreleased]")
        path = root / "schemas/v0/sample.schema.json"
        schema = json.loads(path.read_text())
        schema["required"] = []
        path.write_text(json.dumps(schema) + "\n")
        ledger_contract.ratify(root, "c7.3", {
            "schemas/v0/sample.schema.json": [{"kind": "sample"}],
        })
        self.assertEqual([], check_release(root))

    def test_pyproject_disagreeing_refuses(self) -> None:
        """One mismatch: pyproject alone deviates, and the refusal names all four."""

        root = self._tree(
            version="0.1.0", heading="## [0.1.0] — 2026-08-31", pyproject="9.9.9"
        )
        self.assertIn(
            "spelling_mismatch:module=0.1.0,changelog=0.1.0,tag=0.1.0,pyproject=9.9.9",
            check_release(root, tag="v0.1.0"),
        )

    def test_two_disagreements_are_both_counted(self) -> None:
        """Two mismatches: pyproject and the tag deviate, and both are named."""

        root = self._tree(
            version="0.1.0", heading="## [0.1.0] — 2026-08-31", pyproject="9.9.9"
        )
        mismatched = [
            error
            for error in check_release(root, tag="v0.2.0")
            if error.startswith("spelling_mismatch")
        ]
        self.assertEqual(len(mismatched), 1)
        self.assertIn("pyproject=9.9.9", mismatched[0])
        self.assertIn("tag=0.2.0", mismatched[0])

    def test_four_agreeing_spellings_pass(self) -> None:
        """Zero mismatches: the fourth spelling joins without a false refusal."""

        root = self._tree(version="0.1.0", heading="## [0.1.0] — 2026-08-31")
        self.assertEqual(check_release(root, tag="v0.1.0"), [])

    def test_dated_candidate_qualifies_on_the_ordinary_path(self) -> None:
        root = self._tree(version="0.2.0", heading="## [0.2.0] — 2026-11-26")
        self.assertEqual([], check_release(root, tag="v0.2.0"))
        self.assertEqual([], check_release(root))

    def test_ordinary_dated_candidate_still_checks_each_release_gate(self) -> None:
        for relative, content, error_prefix in (
            ("pyproject.toml", '[project]\nversion = "0.1.0"\n', "spelling_mismatch:"),
            ("README.md", "42 workers [receipt](missing.md).\n",
             "readme_claim_receipt_missing:missing.md"),
            ("floati/records.py", "def validate(row):\n    return False\n",
             "ledger_contract_recut_required:"),
            ("CHANGELOG.md", "# Changelog\n\n## [0.2.0] — soon\n\nLedger contract: `c7.3`.\n",
             "changelog_date_malformed:soon"),
        ):
            with self.subTest(relative=relative):
                root = self._tree(version="0.2.0", heading="## [0.2.0] — 2026-11-26")
                (root / relative).write_text(content, encoding="utf-8")
                for tag in (None, "v0.2.0"):
                    with self.subTest(tag=tag):
                        errors = check_release(root, tag=tag)
                        self.assertTrue(any(error.startswith(error_prefix) for error in errors), errors)

    def test_public_release_copy_refuses_private_repository_links(self) -> None:
        root = self._tree(version="0.2.0", heading="## [0.2.0] — 2026-11-26")
        readme = root / "README.md"
        changelog = root / "CHANGELOG.md"
        dated_notes = changelog.read_text(encoding="utf-8")
        public_link = "[Receipt](https://github.com/Land-o-Clusters/floati/issues/14)\n"
        readme.write_text(public_link, encoding="utf-8")
        self.assertEqual([], check_release(root))
        for document, link in (
            (changelog, "[Receipt](https://github.com/Land-o-Clusters/floati-harbor/blob/main/proof.md)"),
            (readme, "[Receipt]: //github.com/land-o-clusters/floati-harbor/issues/14"),
            (readme, '<a href="https://raw.githubusercontent.com/Land-o-Clusters/floati-harbor/main/README.md">receipt</a>'),
            (changelog, "<https://api.github.com/repos/Land-o-Clusters/floati%2Dharbor/contents/README.md>"),
        ):
            with self.subTest(document=document.name, link=link):
                original = dated_notes if document == changelog else public_link
                document.write_text(original + link + "\n", encoding="utf-8")
                try:
                    for tag in (None, "v0.2.0"):
                        errors = check_release(root, tag=tag)
                        self.assertTrue(any(
                            error.startswith("release_private_repository_link:" + document.name + ":")
                            for error in errors
                        ), errors)
                finally:
                    document.write_text(original, encoding="utf-8")

    def test_pyproject_disagreeing_refuses_without_a_tag(self) -> None:
        """The fourth spelling participates before any tag exists, too."""

        root = self._tree(
            version="0.1.0", heading="## [0.1.0] — 2026-08-31", pyproject="9.9.9"
        )
        self.assertIn(
            "spelling_mismatch:module=0.1.0,changelog=0.1.0,tag=0.1.0,pyproject=9.9.9",
            check_release(root, tag=None),
        )

    def test_absent_pyproject_version_is_typed_not_crashed(self) -> None:
        """A missing or dynamic project version is cited, never filled in."""

        root = self._tree(version="0.1.0", heading="## [0.1.0] — 2026-08-31")
        (root / "pyproject.toml").unlink()
        self.assertEqual(
            check_release(root, tag="v0.1.0"), ["pyproject_version_unreadable"]
        )

    def test_dynamic_pyproject_version_is_typed_not_interpreted(self) -> None:
        """The bounded reader refuses to guess a version the file does not spell."""

        root = self._tree(version="0.1.0", heading="## [0.1.0] — 2026-08-31")
        (root / "pyproject.toml").write_text(
            '[project]\nname = "floati"\ndynamic = ["version"]\n',
            encoding="utf-8",
        )
        self.assertEqual(
            check_release(root, tag="v0.1.0"), ["pyproject_version_unreadable"]
        )

    def test_pyproject_version_after_another_table_is_not_misread(self) -> None:
        """A `version` key belonging to a later table is not `[project]`'s."""

        root = self._tree(version="0.1.0", heading="## [0.1.0] — 2026-08-31")
        (root / "pyproject.toml").write_text(
            '[project]\nname = "floati"\nversion = "0.1.0"\n'
            '[tool.something]\nversion = "9.9.9"\n',
            encoding="utf-8",
        )
        self.assertEqual(pyproject_version(root), "0.1.0")
        self.assertEqual(check_release(root, tag="v0.1.0"), [])

    def test_module_disagreeing_with_changelog_refuses(self) -> None:
        root = self._tree(version="0.1.0", heading="## [0.2.0] — 2026-08-31")
        self.assertIn(
            "spelling_mismatch:module=0.1.0,changelog=0.2.0,tag=0.1.0,pyproject=0.1.0",
            check_release(root, tag="v0.1.0"),
        )

    def test_tag_disagreeing_refuses(self) -> None:
        root = self._tree(version="0.1.0", heading="## [0.1.0] — 2026-08-31")
        self.assertIn(
            "spelling_mismatch:module=0.1.0,changelog=0.1.0,tag=0.2.0,pyproject=0.1.0",
            check_release(root, tag="v0.2.0"),
        )

    def test_unreleased_heading_refuses_when_a_tag_is_named(self) -> None:
        root = self._tree(version="0.1.0", heading="## [0.1.0] — Unreleased")
        self.assertIn("changelog_date_unreleased:0.1.0", check_release(root, tag="v0.1.0"))

    def test_unreleased_heading_passes_before_a_tag_exists(self) -> None:
        """A pull request is not a release; only the pair is checked there."""

        root = self._tree(version="0.1.0", heading="## [0.1.0] — Unreleased")
        self.assertEqual(check_release(root, tag=None), [])

    def test_undated_heading_refuses_when_a_tag_is_named(self) -> None:
        root = self._tree(version="0.1.0", heading="## [0.1.0] — soon")
        self.assertIn("changelog_date_malformed:soon", check_release(root, tag="v0.1.0"))

    def test_tag_without_the_v_prefix_refuses(self) -> None:
        root = self._tree(version="0.1.0", heading="## [0.1.0] — 2026-08-31")
        self.assertIn("tag_malformed:0.1.0", check_release(root, tag="0.1.0"))

    def test_absent_version_assignment_is_typed_not_crashed(self) -> None:
        root = self._tree(version="0.1.0", heading="## [0.1.0] — 2026-08-31")
        (root / "floati" / "__init__.py").write_text("# nothing\n", encoding="utf-8")
        self.assertEqual(check_release(root, tag="v0.1.0"), ["version_unreadable"])

    def test_absent_changelog_heading_is_typed_not_crashed(self) -> None:
        root = self._tree(version="0.1.0", heading="## [0.1.0] — 2026-08-31")
        (root / "CHANGELOG.md").write_text("# Changelog\n\nno sections.\n", encoding="utf-8")
        self.assertEqual(check_release(root, tag="v0.1.0"), ["changelog_heading_missing"])

    def test_first_heading_wins_over_later_sections(self) -> None:
        root = self._tree(version="0.2.0", heading="## [0.2.0] — 2026-09-01")
        with (root / "CHANGELOG.md").open("a", encoding="utf-8") as handle:
            handle.write("\n## [0.1.0] — 2026-08-31\n\nolder.\n")
        self.assertEqual(check_release(root, tag="v0.2.0"), [])
        self.assertEqual(changelog_heading(root), ("0.2.0", "2026-09-01"))

    def test_empty_unreleased_section_keeps_the_candidate_checkable(self) -> None:
        root = self._tree(version="0.2.1", heading="## [0.2.1] - 2026-11-27")
        changelog = root / "CHANGELOG.md"
        dated = changelog.read_text(encoding="utf-8")
        for notes in ("", "Work in progress.\n\nLedger contract: `c7.3`.\n\n"):
            with self.subTest(notes=notes):
                changelog.write_text(dated.replace(
                    "# Changelog\n\n", "# Changelog\n\n## [Unreleased]\n\n" + notes,
                ), encoding="utf-8")
                self.assertEqual([], check_release(root))
                expected = ["changelog_date_unreleased:Unreleased"] if notes else []
                self.assertEqual(expected, check_release(root, tag="v0.2.1"))

    def test_a_readme_claim_without_a_live_receipt_names_each_claim(self) -> None:
        """A receipt link that does not resolve refuses, and both claims are named."""

        root = self._tree(version="0.1.0", heading="## [0.1.0] — 2026-08-31")
        (root / "README.md").write_text(
            "The fleet reconstructs after a kill "
            "([receipt](docs/evidence/missing-a.md)).\n"
            "The soak measured a stop "
            "([receipt](docs/evidence/missing-b.md)).\n",
            encoding="utf-8",
        )
        errors = check_release(root, tag=None)
        self.assertIn(
            "readme_claim_receipt_missing:docs/evidence/missing-a.md", errors
        )
        self.assertIn(
            "readme_claim_receipt_missing:docs/evidence/missing-b.md", errors
        )
        (root / "README.md").write_text("No claim in this file.\n", encoding="utf-8")
        self.assertEqual(
            [error for error in check_release(root, tag=None)
             if error.startswith("readme_claim_receipt_missing:")],
            [],
        )

    def test_a_readme_claim_whose_receipt_file_exists_is_not_refused(self) -> None:
        """A claim whose receipt is a file in the tree is a live link."""

        root = self._tree(version="0.1.0", heading="## [0.1.0] — 2026-08-31")
        receipt = root / "docs" / "evidence" / "live.md"
        receipt.parent.mkdir(parents=True)
        receipt.write_text("measured\n", encoding="utf-8")
        (root / "README.md").write_text(
            "Held ([receipt](docs/evidence/live.md)).\n",
            encoding="utf-8",
        )
        self.assertEqual(
            [
                error
                for error in check_release(root, tag=None)
                if error.startswith("readme_claim_receipt_missing:")
            ],
            [],
        )

    def test_new_unlinked_numeric_claim_refuses_release(self) -> None:
        root = self._tree(version="0.1.0", heading="## [0.1.0] — Unreleased")
        readme = root / "README.md"
        readme.write_text("A quiet fleet.\n", encoding="utf-8")
        self.assertEqual(check_release(root), [])
        readme.write_text("A quiet fleet.\nIt serves 42 workers.\n", encoding="utf-8")
        self.assertIn("readme_claim_unlinked:2:It serves 42 workers.", check_release(root))

    def test_claim_units_require_their_own_local_receipt(self) -> None:
        root = self._tree(version="0.1.0", heading="## [0.1.0] — Unreleased")
        (root / "receipt.md").write_text("Evidence.\n", encoding="utf-8")
        cases = [
            ("It serves 42 workers.", 1),
            ("It serves 42.", 1),
            ("MEASURED fast.", 1), ("DERIVED fast.", 1), ("ESTIMATE fast.", 1),
            ("It serves 42 workers [receipt](receipt.md).", 0),
            ("It serves 42\nworkers [receipt](receipt.md#details).", 0),
            ("It serves 42 workers [proof][receipt].\n\n[receipt]: receipt.md", 0),
            ("It serves 42 workers. Evidence [here](receipt.md).", 1),
            ("**It serves 42 workers.** Evidence [here](receipt.md).", 1),
            ('"It serves 42 workers." Evidence [here](receipt.md).', 1),
            ("It serves 42 workers [web](https://example.org/proof).", 1),
            ("It serves 42 workers ![image](receipt.md).", 1),
            ("It serves 42 workers `[receipt](receipt.md)`.", 1),
            ("| Reader | Median |\n|---|---|\n| [inbox](receipt.md) | 34 ms |", 1),
            ("Metric | Receipt\n--- | ---\n42 workers | [proof](receipt.md)", 1),
            ("harness | wake\n--- | ---\ntool | live", 1),
            ("| harness | bus | work | wake |\n|---|---|---|---|\n| tool | live | — | n/a |", 3),
            ("| harness | wake |\n|---|---|\n| tool | [daemon](receipt.md) ○ |", 0),
            ("| harness | wake |\n|---|---|\n| tool | event-driven ● |", 1),
            ("| harness / surface | wake | notes |\n|---|---|---|\n| tool | daemon ○ | uses --harness tool |\n| next | live | |", 2),
            ("~~~text\n42 workers MEASURED.\n~~~", 0),
            ("```text\n42 workers.\n```", 0),
            ("<!--\n42 workers MEASURED.\n-->", 0),
            ("It serves <!-- annotation -->42 workers.", 1),
            ("<img src=\"receipt.md\" width=\"120\">", 0),
            ("A [guide](docs/version-42.md) is elsewhere.", 0),
            ("Issue [#14](https://example.org/issues/14) is open.", 0),
            ("Version v0.1.2 is current.", 0),
            ("Python 3.9 or newer.", 0),
            ("Product code is licensed under Apache-2.0.", 0),
            ("It uses SHA-256 and MX-1 identifiers.", 0),
            ("Run `umask 022`.", 0),
            ("On 2026-10-27 it shipped.", 0),
            ("1. Install the tool.\n2. Read the guide.", 0),
            ("python3 -m floati install --ref v0.1.2", 0),
            ("`floati send --note 42`", 0),
            ("It serves 10,000 workers.", 1),
            ("It takes 0.25 seconds.", 1),
            ("It takes 42ms.", 1),
            ("It uses 8GB.", 1),
            ("42 workers [proof](https://[broken).", 1),
            ("It serves `42` workers.", 1),
            ("`MEASURED` fast.", 1),
            ("`42 ms`", 1),
            ("Version v0.1.2 serves 42 workers on 2026-10-27.", 1),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                (root / "README.md").write_text(source + "\n", encoding="utf-8")
                findings = [f for f in check_release(root) if f.startswith("readme_claim_unlinked:")]
                self.assertEqual(len(findings), expected, findings)

    def test_allowlist_is_exact_and_stale_entries_refuse(self) -> None:
        root = self._tree(version="0.1.0", heading="## [0.1.0] — Unreleased")
        unit = "It serves 42 workers."
        (root / "README.md").write_text("It serves  42\nworkers.\n", encoding="utf-8")
        allowlist = root / "docs/release/readme-claims-allowlist.v0.json"
        allowlist.parent.mkdir(parents=True)
        allowlist.write_text(json.dumps([{
            "claim_digest": hashlib.sha256(unit.encode()).hexdigest(),
            "excerpt": unit, "reason": "reviewed fixture",
        }]), encoding="utf-8")
        self.assertEqual(check_release(root), [])
        (root / "README.md").write_text("It serves 43 workers.\n", encoding="utf-8")
        errors = check_release(root)
        self.assertTrue(any(f.startswith("readme_claim_unlinked:") for f in errors))
        self.assertTrue(any(f.startswith("readme_claim_allowlist_stale") for f in errors))

    def test_teardown_verdicts_need_their_own_receipt_or_an_exact_typed_absence(self) -> None:
        """A nonnumeric yes/no is a claim; an absence must not smuggle one in."""

        root = self._tree(version="0.1.0", heading="## [0.1.0] — Unreleased")
        (root / "receipt.md").write_text("Native observation.\n", encoding="utf-8")
        for column in ("Worker teardown", "Observed lifecycle", "Teardown"):
            for cell, expected, is_claim in (
                ("", 1, True),
                ("Required", 1, True),
                ("Not needed", 1, True),
                ("Explicit teardown completed", 1, True),
                ("NOT_YET_MEASURED: `worker_omission_pair_missing`", 0, False),
                ("NOT_YET_MEASURED", 1, True),
                ("NOT_YET_MEASURED: `worker_omission_pair_missing`; not needed", 1, True),
                ("NOT_YET_MEASURED: `worker_omission_pair_missing` [not needed](receipt.md)", 0, True),
                ("[Explicit teardown completed](receipt.md)", 0, True),
            ):
                with self.subTest(column=column, cell=cell):
                    (root / "README.md").write_text(
                        "| Harness | %s |\n| --- | --- |\n| cursor | %s |\n" % (column, cell),
                        encoding="utf-8",
                    )
                    findings = [f for f in check_release(root)
                                if f.startswith("readme_claim_unlinked:")]
                    expected_unlinked = int(is_claim) if column == "Worker teardown" else expected
                    self.assertEqual(expected_unlinked, len(findings), findings)
                    self.assertEqual(is_claim, any(c["text"] == cell
                                                  for c in readme_claim_census(root)))
        (root / "README.md").write_text(
            "| Harness | Worker teardown | Observed lifecycle |\n"
            "| --- | --- | --- |\n"
            "| cursor | NOT_YET_MEASURED: `worker_omission_pair_missing` |\n",
            encoding="utf-8",
        )
        findings = [f for f in check_release(root) if f.startswith("readme_claim_unlinked:")]
        self.assertEqual(1, len(findings), "the omitted trailing verdict still needs evidence")

    def test_worker_teardown_verdict_replays_both_native_arms(self) -> None:
        root = self._tree(version="0.1.0", heading="## [0.1.0] — Unreleased")
        receipt = root / "docs/evidence/conformance/worker-teardown.json"
        receipt.parent.mkdir(parents=True)
        link = receipt.relative_to(root).as_posix()

        def arm(previous, successor):
            consent = "consent-" + successor
            return {
                "previous_session": previous,
                "next_session": successor,
                "transcript_sha256": ("b" if previous == "omit-old" else "c") * 64,
                "after_close": {
                    "claim_session": None, "consent_armed": False,
                    "hook_active": False, "pending_mail": ["msg-probe"],
                },
                "boot": {"status": "ok", "evidence": {
                    "node_id": "worker", "acting_session_id": successor, "harness": "cursor",
                    "board": {"step": "completed", "item_ids": ["msg-probe"], "node_id": "worker",
                              "acting_session_id": successor, "id": "board-" + successor,
                              "arm_receipt_id": "arm-" + successor,
                              "delivery_receipt_id": "delivery-" + successor},
                    "consent_receipt_id": consent,
                    "hook": {"state": "verified"},
                    "first_hold": {"id": "hold-" + successor, "phase": "start", "node_id": "worker",
                                   "harness": "cursor", "acting_session_id": successor,
                                   "consent_receipt_id": consent},
                }},
            }

        def pair():
            control = arm("control-old", "control-new")
            control["teardown"] = {"status": "ok", "evidence": {
                "node_id": "worker", "acting_session_id": "control-old", "harness": "cursor",
                "teardown": {"id": "teardown-control", "step": "completed"},
                "activation": {"state": "disarmed", "consent_receipt_id": "consent-disarmed"},
            }}
            return {
                "source_sha": "a" * 40, "native_version": "1.0", "harness": "cursor",
                "node": "worker", "omission": arm("omit-old", "omit-new"), "control": control,
            }

        def blocked(data):
            data["omission"]["after_close"]["claim_session"] = "omit-old"
            data["omission"]["after_refusal"] = {"pending_mail": ["msg-probe"]}
            data["omission"]["boot"] = {
                "status": "refused", "evidence": {"code": "seat_board_claim_contested",
                                                  "remedy": "pass --take-over to replace claimant omit-old"},
            }
            return data

        required = blocked(pair())
        wrong_reason = blocked(pair())
        wrong_reason["omission"]["boot"]["evidence"]["code"] = "node_boot_workspace_unrooted"
        reused_session = pair()
        reused_session["omission"]["next_session"] = "omit-old"
        lost_mail = pair()
        lost_mail["omission"]["boot"]["evidence"]["board"]["item_ids"] = []
        missing_control = pair()
        del missing_control["control"]["teardown"]
        stale_consent = pair()
        stale_consent["omission"]["after_close"]["consent_armed"] = True
        unrelated_claim = blocked(pair())
        unrelated_claim["omission"]["after_close"]["claim_session"] = "other-session"
        for harness, label, data, passed in (
            ("cursor", "No teardown needed", pair(), True),
            ("cursor", "Teardown required", required, True),
            ("cursor", "No teardown needed", {}, False),
            ("cursor", "No teardown needed", missing_control, False),
            ("cursor", "No teardown needed", reused_session, False),
            ("cursor", "No teardown needed", lost_mail, False),
            ("cursor", "No teardown needed", stale_consent, False),
            ("cursor", "Teardown required", unrelated_claim, False),
            ("cursor", "Teardown required", wrong_reason, False),
            ("cursor", "No teardown needed", required, False),
            ("claude", "No teardown needed", pair(), False),
        ):
            with self.subTest(harness=harness, label=label, passed=passed):
                receipt.write_text(json.dumps(data), encoding="utf-8")
                (root / "README.md").write_text(
                    "| Harness | Worker teardown |\n| --- | --- |\n"
                    f"| {harness} | [{label}]({link}) |\n", encoding="utf-8",
                )
                claim, = readme_claim_census(root)
                self.assertEqual(passed, claim["linked"])
                self.assertEqual(passed, not check_release(root))

        allowlist = root / "docs/release/readme-claims-allowlist.v0.json"
        allowlist.parent.mkdir(parents=True)
        allowlist.write_text(json.dumps([{
            "claim_digest": claim["claim_digest"],
            "excerpt": claim["text"][:80], "reason": "fixture exemption",
        }]), encoding="utf-8")
        self.assertTrue(any(f.startswith("readme_claim_unlinked:") for f in check_release(root)))

    def test_readme_publishes_worker_teardown_posture_for_each_stop_hook_harness(self) -> None:
        """The public README exposes the posture even before native pairs exist."""

        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        table = readme.split("### Worker teardown", 1)
        self.assertEqual(2, len(table), "the per-harness teardown table is missing")
        table = table[1].split("\n## ", 1)[0]
        for harness in ("codex", "claude", "cursor", "zcode"):
            with self.subTest(harness=harness):
                self.assertRegex(table, r"(?m)^\| " + harness + r" \|")

    def test_allowlist_cannot_hide_invalid_entries_or_external_receipts(self) -> None:
        root = self._tree(version="0.1.0", heading="## [0.1.0] — Unreleased")
        allowlist = root / "docs/release/readme-claims-allowlist.v0.json"
        allowlist.parent.mkdir(parents=True)
        for data in ("broken", "{}", '[{"claim_digest": "bad", "excerpt": "x", "reason": ""}]'):
            with self.subTest(data=data):
                allowlist.write_text(data, encoding="utf-8")
                self.assertIn("readme_claim_allowlist_invalid", check_release(root))
        allowlist.unlink()
        with __import__("tempfile").TemporaryDirectory() as outside:
            target = Path(outside) / "receipt.md"
            target.write_text("Proof.\n", encoding="utf-8")
            (root / "escape.md").symlink_to(target)
            for reference in (str(target), "escape.md"):
                with self.subTest(reference=reference):
                    (root / "README.md").write_text(
                        "42 workers [receipt](%s).\n" % reference, encoding="utf-8"
                    )
                    self.assertTrue(any(f.startswith("readme_claim_unlinked:") for f in check_release(root)))
    def test_missing_receipt_does_not_exempt_a_numeric_claim(self) -> None:
        root = self._tree(version="0.1.0", heading="## [0.1.0] — Unreleased")
        (root / "README.md").write_text("42 workers [receipt](missing.md).\n", encoding="utf-8")
        errors = check_release(root)
        self.assertIn("readme_claim_receipt_missing:missing.md", errors)
        self.assertTrue(any(f.startswith("readme_claim_unlinked:") for f in errors))

    def test_readme_claim_exceptions_have_completed_review(self) -> None:
        allowlist = REPOSITORY_ROOT / "docs/release/readme-claims-allowlist.v0.json"
        rows = json.loads(allowlist.read_text(encoding="utf-8"))
        self.assertFalse([row for row in rows if row["reason"] == "pre-census: owner review"])

    def test_this_repository_spells_its_version_the_same_way_twice(self) -> None:
        """The standing guard: the module, the heading and pyproject agree."""

        from scripts import ledger_contract

        status_command = ["/usr/bin/git", "status", "--porcelain", "--untracked-files=all"]
        before = subprocess.check_output(status_command, cwd=REPOSITORY_ROOT, text=True)
        inventory = ledger_contract.inventory

        def observe_inventory(root):
            # The contract check holds its lock while it inventories. Another
            # test copying or checking this checkout at that moment must find
            # it exactly as it was.
            self.assertEqual(before, subprocess.check_output(status_command, cwd=REPOSITORY_ROOT, text=True),
                             "the repository release guard wrote into the running checkout")
            return inventory(root)

        # The check takes a lock inside the tree it checks. Check a private
        # copy of today's bytes so no other test can meet that lock.
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            candidate = copy_live_contract_inputs(ledger_contract, Path(directory) / "live-copy")
            check = ledger_contract.check
            with mock.patch.object(ledger_contract, "inventory", side_effect=observe_inventory),\
                    mock.patch.object(ledger_contract, "check", side_effect=lambda _root: check(candidate)):
                self.assertEqual(check_release(REPOSITORY_ROOT, tag=None), [])
        claims = readme_claim_census(REPOSITORY_ROOT)
        # Filling a measured teardown cell is an evidence edit, not a new
        # claim-count/digest baseline. The unit properties above guard census
        # discovery; this repository guard requires actual receipt coverage.
        self.assertTrue(claims, "the README census must discover claims")
        self.assertEqual([], [c['text'] for c in claims if not c['linked']])
        self.assertIn(changelog_heading(REPOSITORY_ROOT)[0],
                      ("Unreleased", module_version(REPOSITORY_ROOT)))
        self.assertEqual(pyproject_version(REPOSITORY_ROOT), module_version(REPOSITORY_ROOT))


if __name__ == "__main__":
    unittest.main()

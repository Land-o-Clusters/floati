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
"""

from __future__ import annotations

import unittest
from pathlib import Path

from floati.release import check_release, changelog_heading, module_version


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ReleaseSpellingCheckTests(unittest.TestCase):
    """Three spellings of one value, and a typed refusal for every way to disagree."""

    def _tree(self, *, version: str, heading: str) -> Path:
        import tempfile

        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root, True)
        (root / "floati").mkdir()
        (root / "floati" / "__init__.py").write_text(
            '"""Floati protocol core."""\n\n__version__ = "%s"\n' % version,
            encoding="utf-8",
        )
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n%s\n\nprose.\n" % heading, encoding="utf-8"
        )
        return root

    def test_three_agreeing_spellings_pass(self) -> None:
        root = self._tree(version="0.1.0", heading="## [0.1.0] — 2026-08-31")
        self.assertEqual(check_release(root, tag="v0.1.0"), [])

    def test_module_disagreeing_with_changelog_refuses(self) -> None:
        root = self._tree(version="0.1.0", heading="## [0.2.0] — 2026-08-31")
        self.assertIn(
            "spelling_mismatch:module=0.1.0,changelog=0.2.0,tag=0.1.0",
            check_release(root, tag="v0.1.0"),
        )

    def test_tag_disagreeing_refuses(self) -> None:
        root = self._tree(version="0.1.0", heading="## [0.1.0] — 2026-08-31")
        self.assertIn(
            "spelling_mismatch:module=0.1.0,changelog=0.1.0,tag=0.2.0",
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

    def test_this_repository_spells_its_version_the_same_way_twice(self) -> None:
        """The standing guard: `__version__` and the top changelog heading agree."""

        self.assertEqual(check_release(REPOSITORY_ROOT, tag=None), [])
        self.assertEqual(
            module_version(REPOSITORY_ROOT), changelog_heading(REPOSITORY_ROOT)[0]
        )


if __name__ == "__main__":
    unittest.main()

"""DOCS-GLOSSARY-1: every Floati-specific term on the public surface has an entry.

Property (docs/status/ROWS.md; cut 2026-10-09 on the owner's word "Agreed -
add the row for the glossary"): a stranger can read every Floati-specific
term on the public surface without reading code. Each term has a glossary
entry giving (1) one plain sentence, (2) the nearest standard term — pub/sub,
queue, lock, heartbeat, at-least-once delivery — or *no standard equivalent*,
and (3) where Floati's meaning differs from that term. The entries keep
DESIGN Law 14's separations: a lease is never liveness, delivery is not
consumption, an acknowledgment is not completion. The glossary lives at
docs/glossary.md, ships in the public projection, and is linked from the
README at its first vocabulary use. No term is renamed.

The term set is DERIVED from the product's own enumerations, never typed:
the live CLI parser's top-level verbs and full command paths
(`floati.cli._parser`, walked the way `floati.command_contract` walks it),
the output contract's status words (`floati.cli._EXIT_CODE_CONTRACT`), and
the record kinds `floati.records.validate_record` compares against (read as
an AST, the technique `tests/test_rem_2.py` established). A derived term is
ON the public surface when README.md uses it — the README is the public
surface this row anchors (the glossary is "linked from the README at its
first vocabulary use", and docs/ ships wholesale behind it). A surface term
with no `### <term>` entry in docs/glossary.md fails this test, so a new
term without an entry reds.

The test pins presence and never words: rewording an entry's sentences —
the same term, a different voice — stays green. The three-part entry shape
is the author's contract and the architect's to judge, not the test's.

Ways this check could be wrong, each with a case below:

1. A hand-typed term list. Built: every term comes from the live parser,
   the exit-code contract, or an AST over records.py at this tree.
2. A vacuous census (a broken derivation, zero terms, green forever).
   Built: a printed floor on both the derived and the surface population.
3. Pinning voice. Built: two glossary texts differing only in entry bodies
   yield the same term set.
4. Insensitivity. Built: planting a synthetic term on the surface makes the
   census report it missing; removing the plant clears it.
"""

from __future__ import annotations

import argparse
import ast
import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GLOSSARY_PATH = REPOSITORY_ROOT / "docs" / "glossary.md"
README_PATH = REPOSITORY_ROOT / "README.md"

# Vacuity floors: the derivation must never silently empty. Measured
# 2026-10-11 at main 759c2033: 303 derived, 59 on the README surface.
DERIVED_FLOOR = 100
SURFACE_FLOOR = 20


def _parser_verbs() -> frozenset:
    """Top-level verbs and full command paths of the live parser."""

    from floati.cli import _parser

    paths = []
    top_level = set()

    def walk(parser, prefix):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    paths.append(prefix + [name])
                    if not prefix:
                        top_level.add(name)
                    walk(child, prefix + [name])

    walk(_parser(), [])
    return frozenset({" ".join(parts) for parts in paths} | top_level)


def _status_words() -> frozenset:
    """The output contract's artifact states (AGENTS.md: exit codes table)."""

    from floati.cli import _EXIT_CODE_CONTRACT

    return frozenset(row["status"] for row in _EXIT_CODE_CONTRACT)


def _record_kinds() -> frozenset:
    """Every kind value validate_record compares against, read as an AST."""

    source = (REPOSITORY_ROOT / "floati" / "records.py").read_text(
        encoding="utf-8"
    )
    kinds = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "kind"
        ):
            for comparator in node.comparators:
                for inner in ast.walk(comparator):
                    if isinstance(inner, ast.Constant) and isinstance(
                        inner.value, str
                    ):
                        kinds.add(inner.value)
    return frozenset(kinds)


def derived_terms() -> frozenset:
    return _parser_verbs() | _status_words() | _record_kinds()


def _used_on_readme(term: str, readme_text: str) -> bool:
    pattern = r"(?<![A-Za-z0-9_-])" + re.escape(term) + r"(?![A-Za-z0-9_-])"
    return re.search(pattern, readme_text) is not None


def surface_terms(readme_text: str = None) -> frozenset:
    if readme_text is None:
        readme_text = README_PATH.read_text(encoding="utf-8")
    return frozenset(
        term
        for term in derived_terms()
        if _used_on_readme(term, readme_text)
    )


def glossary_terms(text: str = None) -> frozenset:
    """The terms docs/glossary.md defines, one `### <term>` heading each."""

    if text is None:
        if not GLOSSARY_PATH.exists():
            return frozenset()
        text = GLOSSARY_PATH.read_text(encoding="utf-8")
    return frozenset(
        match.group(1).strip()
        for match in re.finditer(r"^###\s+(.+?)\s*$", text, re.MULTILINE)
    )


class GlossaryCensusTests(unittest.TestCase):
    def test_every_public_surface_term_has_an_entry(self) -> None:
        """The property: no Floati-specific term the README uses goes undefined."""

        missing = sorted(surface_terms() - glossary_terms())
        self.assertEqual(
            [],
            missing,
            "terms the README uses with no docs/glossary.md entry",
        )

    def test_derivation_is_not_vacuous(self) -> None:
        """A derivation that silently empties would green a broken census."""

        self.assertGreaterEqual(len(derived_terms()), DERIVED_FLOOR)
        self.assertGreaterEqual(len(surface_terms()), SURFACE_FLOOR)

    def test_planted_term_reds_then_is_removed(self) -> None:
        """Plant a derived term on the surface: the census names it; remove: it clears."""

        from unittest import mock

        planted = "glossary_census_plant"
        readme = README_PATH.read_text(encoding="utf-8")
        real = derived_terms()
        self.assertNotIn(planted, real)
        self.assertNotIn(planted, glossary_terms())
        with mock.patch(
            "%s.derived_terms" % __name__, return_value=real | {planted}
        ):
            with_plant = surface_terms(readme_text=readme + "\n`%s`\n" % planted)
            self.assertIn(planted, with_plant)
            self.assertIn(planted, sorted(with_plant - glossary_terms()))
        self.assertNotIn(planted, surface_terms(readme_text=readme))

    def test_reworded_entries_stay_green(self) -> None:
        """The census pins presence and never words."""

        base = (
            "# Glossary\n\n### glossary_census_plant\n\n"
            "One plain sentence.\n\nNearest standard term: queue.\n"
        )
        reworded = (
            "# Glossary\n\n### glossary_census_plant\n\n"
            "A wholly different sentence in another voice, same term.\n\n"
            "Nearest standard term: topic.\n"
        )
        self.assertEqual(glossary_terms(base), glossary_terms(reworded))
        self.assertIn("glossary_census_plant", glossary_terms(base))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from floati import fixture_ids as public_ids
from floati.identity_fence import (
    RETIRED_PRODUCT_NAME as RETIRED,
    RETIRED_PRODUCT_SHORT_NAME as RETIRED_SHORT,
)

import importlib.util
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from floati.identity_fence import HOME_PREFIX, OWNER_USERNAME
from tests.export_inventory import (
    POLICY_RELATIVE,
    classify_inventory,
    export_policy,
    export_include_set,
    materialise_exposed_tree,
    published_baseline,
)
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

def _tracked_text_files() -> tuple[str, ...]:
    """Return the Git publication inventory, so the sweep cannot go stale."""

    listing = subprocess.run(
        ["git", "ls-files", "-z", "--cached"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return tuple(
        entry.decode("utf-8", errors="replace")
        for entry in listing.split(b"\0")
        if entry
    )


def _exposure_adapter():
    """Return the exporter's text adaptation, or None in the public projection.

    Am.4. The tree fence's question is *what would a reader of the published
    artifact see*, and the exporter answers half of it: recorded invocations,
    `--repo` values and module coordinates are PHOTOGRAPHS, redacted at exposure
    and never rewritten at source (Am.1, Am.2). A fence that reads the harbor's
    raw bytes therefore reds on 22 exported evidence documents the publication
    instrument passes -- and the publication instrument is the authority. So the
    same file is read the way the export would expose it.

    In the projection the exporter is absent AND the tree is already adapted, so
    there is nothing to apply and nothing to miss.
    """

    if not (REPOSITORY_ROOT / "scripts" / "export_public.py").is_file():
        return None
    return importlib.import_module("scripts.export_public")._adapt


def retired_name_offenders(root: Path, relatives: tuple[str, ...]) -> list[str]:
    """Return `path:line` for each supplied file carrying the retired name.

    Split out from the tree fence so a control can point the SAME scanner at a
    fixture tree. A file the population names but the tree does not carry is not
    a finding here: the population is derived from path strings alone, which is
    what lets a control classify a planted path without planting anything.
    """

    adapt = _exposure_adapter()
    offenders: list[str] = []
    for relative in relatives:
        try:
            data = (root / relative).read_bytes()
        except OSError:
            continue
        if adapt is not None:
            try:
                data, _notes = adapt(relative, data)
            except Exception:  # pragma: no cover - an adapter refusal is not a name finding
                pass
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        match = RETIRED_NAME_AT_SOURCE.search(text)
        if match is not None:
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{relative}:{line}")
    return sorted(offenders)


PRIVATE_ACCOUNT_PATTERNS = (
    re.compile(re.escape(HOME_PREFIX) + r"[^/\s`<>]+", re.IGNORECASE),
    re.compile(re.escape(str(Path.home())), re.IGNORECASE),
    re.compile(re.escape(OWNER_USERNAME), re.IGNORECASE),
)

TENANT_PATTERNS = (
    *PRIVATE_ACCOUNT_PATTERNS,
    re.compile(r"CMs-M5", re.IGNORECASE),
    re.compile(rf"\.{RETIRED}-bus/{re.escape(PRIVATE_FLEET)}", re.IGNORECASE),
    re.compile(re.escape(PRIVATE_FLEET), re.IGNORECASE),
    re.compile(r"~/Projects/puddle", re.IGNORECASE),
    re.compile(rf"/absolute/{RETIRED}", re.IGNORECASE),
)

RETIRED_PRODUCT_NAME = re.compile(
    rf"(?<![\w./-])(?:{RETIRED}|{RETIRED_SHORT})(?!\w)",
    re.IGNORECASE,
)

# R-N4 Am.4. The LIVING-DOCUMENT predicate above and the TREE predicate below
# are deliberately different, and the difference is the short form.
#
# The short form is an ordinary English word. Prose written today may not teach
# the retired verb at all, so the living docs keep the strict both-forms match.
# But a photographed transcript is not prose: `add <short> v0 artifact CLI` in a
# recorded commit subject is English, and the publication instrument passes it.
# The exporter already owns the rule for when the short form IS the product --
# `RETIRED_SHORT_COORDINATE`, ruled in Am.2: followed by a verb, a dot-suffix, a
# slash, or after `-m`. ONE definition of what counts, imported rather than
# restated, so the two instruments cannot drift.
#
# The full name keeps its bare-word match, with R-N2's exemptions carried by the
# lookbehind: `.<name>` and `x-<name>-` are on-disk coordinates the product
# READS and must keep.
_FULL_NAME_AT_SOURCE = rf"(?<![\w./-]){RETIRED}(?!\w)"


def _tree_predicate() -> "re.Pattern[str]":
    exporter_path = REPOSITORY_ROOT / "scripts" / "export_public.py"
    if not exporter_path.is_file():
        # The projection: no exporter, and its tree is already adapted, so every
        # coordinate form the short-form rule names has been redacted away.
        return re.compile(_FULL_NAME_AT_SOURCE, re.IGNORECASE)
    coordinate = importlib.import_module(
        "scripts.export_public"
    ).RETIRED_SHORT_COORDINATE
    return re.compile(
        rf"(?:{_FULL_NAME_AT_SOURCE})|(?:{coordinate.pattern})", re.IGNORECASE
    )


RETIRED_NAME_AT_SOURCE = _tree_predicate()


class NameSweepLauncherTests(unittest.TestCase):
    def test_floati_is_canonical_regular_executable(self) -> None:
        """Catches a missing, symlinked, or non-executable public launcher."""
        launcher = REPOSITORY_ROOT / "scripts" / "floati"

        self.assertTrue(launcher.is_file())
        self.assertFalse(launcher.is_symlink())
        self.assertTrue(launcher.stat().st_mode & 0o111)

    def test_transition_alias_is_absent(self) -> None:
        """Catches a retained compatibility launcher or symlink."""
        launcher = REPOSITORY_ROOT / "scripts" / RETIRED_SHORT
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
        self.assertNotRegex(completed.stdout, rf"(?<![./])\b{RETIRED_SHORT}\b")
        self.assertNotIn(RETIRED.capitalize(), completed.stdout)


class RetiredNameScrubVocabularyTests(unittest.TestCase):
    """The scrub half: the token is IN the vocabulary and the scanners fire on it.

    Proved on a scratch tree rather than by reading the source, because a token
    present in a tuple proves only that a tuple has a token in it.
    """

    def _fixture(self, root: Path) -> None:
        (root / "carrier.bin").write_bytes(bytes.fromhex("736c6970776179"))
        (root / "neutral.bin").write_bytes(b"floati")

    def test_generated_tree_scrub_detects_the_retired_name(self) -> None:
        from floati.scrub import scan_generated_tree, scan_generated_tree_by_code

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._fixture(root)

            self.assertEqual(["carrier.bin"], scan_generated_tree(root))
            self.assertEqual(
                {"private_project_name": [], "retired_product_name": ["carrier.bin"]},
                scan_generated_tree_by_code(root),
            )

    def test_generated_tree_scrub_reports_a_clean_tree_clean(self) -> None:
        """Control: the finding above is the token, not any byte in a .bin."""

        from floati.scrub import scan_generated_tree

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._fixture(root)
            (root / "carrier.bin").unlink()

            self.assertEqual([], scan_generated_tree(root))

    def test_public_name_fence_names_the_retired_token_distinctly(self) -> None:
        """Two names, two remedies: folding them into one code misdirects."""

        specification = importlib.util.spec_from_file_location(
            "name_sweep_public_fence", REPOSITORY_ROOT / "scripts" / "public_name_fence.py"
        )
        assert specification is not None and specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._fixture(root)

            self.assertEqual(
                [{"code": "retired_product_name", "path": "carrier.bin"}],
                module.scan_tree(root),
            )


class RetiredNameSiteAllowlistTests(unittest.TestCase):
    """The SITE allowance: enumerated coordinate forms, at named files, by count.

    The product still emits three shapes that carry the retired name and cannot
    be renamed -- a dot-prefixed workspace directory the migration refusal reads,
    a retired schema-extension prefix, and a retired schema origin -- and the
    evidence that records the migration has to quote them. The allowance names
    the file AND the exact form AND how many times, so the fence still refuses
    the bare word in those same files.

    Every case below runs the real scanner over real bytes: the fixture is the
    repository's own evidence file with its UNENUMERATED hits redacted by a
    derivation, not a hand-written imitation of one.
    """

    TOKEN = RETIRED.encode("ascii")

    def _rows(self, relative: str) -> tuple[tuple[object, ...], ...]:
        from floati.scrub import RETIRED_NAME_SITE_ALLOWLIST

        return tuple(
            row for row in RETIRED_NAME_SITE_ALLOWLIST if row[1] == relative
        )

    def _redact_unenumerated(self, raw: bytes, relative: str) -> bytes:
        """Return the file with every hit the allowance does NOT enumerate removed.

        This is what the file looks like once NAME-2d has scrubbed its prose. The
        enumerated coordinates are located by finding each form and keeping the
        token occurrence inside it; every other occurrence becomes the current
        name, exactly as a prose scrub would leave it.
        """

        lowered = raw.lower()
        keep: set[int] = set()
        for _code, _path, form, _count, _reason in self._rows(relative):
            start = 0
            while True:
                found = lowered.find(form.lower(), start)
                if found < 0:
                    break
                keep.add(lowered.find(self.TOKEN, found))
                start = found + len(form)
        out = bytearray()
        index = 0
        while True:
            found = lowered.find(self.TOKEN, index)
            if found < 0:
                out += raw[index:]
                break
            out += raw[index:found]
            out += self.TOKEN if found in keep else b"floati"
            index = found + len(self.TOKEN)
        return bytes(out)

    def _tree(self, stack, relative: str, raw: bytes) -> Path:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return root

    def test_every_allowlisted_site_carries_exactly_its_pinned_count(self) -> None:
        """The pin, measured against the repository rather than against itself."""

        from floati.scrub import RETIRED_NAME_SITE_ALLOWLIST

        self.assertEqual(5, len(RETIRED_NAME_SITE_ALLOWLIST))
        self.assertEqual(25, sum(row[3] for row in RETIRED_NAME_SITE_ALLOWLIST))
        for code, relative, form, expected, reason in RETIRED_NAME_SITE_ALLOWLIST:
            with self.subTest(relative=relative, form=form):
                self.assertEqual("retired_product_name", code)
                self.assertTrue(reason)
                self.assertIn(self.TOKEN, form)
                self.assertNotEqual(self.TOKEN, form)
                path = REPOSITORY_ROOT / relative
                self.assertTrue(path.is_file())
                self.assertEqual(
                    expected, path.read_bytes().lower().count(form.lower())
                )

    def test_a_file_of_enumerated_coordinates_is_cleared_and_would_not_be_without_it(
        self,
    ) -> None:
        """RED first, then GREEN: the same bytes, with and without the allowance."""

        from contextlib import ExitStack
        from unittest import mock

        from floati.scrub import scan_generated_tree

        relative = "docs/evidence/FL4.5-FLOATI-INTERNAL-RENAME.md"
        raw = self._redact_unenumerated(
            (REPOSITORY_ROOT / relative).read_bytes(), relative
        )
        self.assertEqual(11, raw.lower().count(self.TOKEN))

        with ExitStack() as stack:
            root = self._tree(stack, relative, raw)
            with mock.patch("floati.scrub.RETIRED_NAME_SITE_ALLOWLIST", ()):
                self.assertEqual([relative], scan_generated_tree(root))
            self.assertEqual([], scan_generated_tree(root))

    def test_a_bare_word_planted_in_an_allowlisted_file_still_fires(self) -> None:
        """The allowance narrows WHICH hits are tolerated; it does not stop reading."""

        from contextlib import ExitStack

        from floati.scrub import scan_generated_tree

        relative = "docs/evidence/HM1B-LIVE-WORKERS.md"
        clean = self._redact_unenumerated(
            (REPOSITORY_ROOT / relative).read_bytes(), relative
        )
        with ExitStack() as stack:
            root = self._tree(stack, relative, clean)
            self.assertEqual([], scan_generated_tree(root))

            planted = root / relative
            planted.write_bytes(clean + b"\nthe " + self.TOKEN + b" rename\n")
            self.assertEqual([relative], scan_generated_tree(root))

            planted.write_bytes(clean)
            self.assertEqual([], scan_generated_tree(root))

    def test_a_coordinate_count_that_moves_is_a_finding_in_both_directions(self) -> None:
        """The count is the pin: a thirteenth coordinate and a missing twelfth both RED."""

        from contextlib import ExitStack

        from floati.scrub import scan_generated_tree

        relative = "docs/evidence/HM05-DOGFOOD.md"
        clean = self._redact_unenumerated(
            (REPOSITORY_ROOT / relative).read_bytes(), relative
        )
        coordinate = b"." + self.TOKEN
        self.assertEqual(12, clean.lower().count(coordinate))

        with ExitStack() as stack:
            root = self._tree(stack, relative, clean)
            target = root / relative
            self.assertEqual([], scan_generated_tree(root))

            target.write_bytes(clean + b"\n" + coordinate + b"-extra\n")
            self.assertEqual([relative], scan_generated_tree(root))

            target.write_bytes(clean.replace(coordinate, b".floati", 1))
            self.assertEqual([relative], scan_generated_tree(root))

    def test_the_allowance_is_a_site_not_a_form(self) -> None:
        """Control: the same coordinate at an unlisted path is still a finding."""

        from contextlib import ExitStack

        from floati.scrub import scan_generated_tree

        with ExitStack() as stack:
            root = self._tree(
                stack, "docs/evidence/UNLISTED.md", b"." + self.TOKEN + b"-bus\n"
            )
            self.assertEqual(["docs/evidence/UNLISTED.md"], scan_generated_tree(root))


class NameSweepLivingDocumentationTests(unittest.TestCase):
    def test_retired_product_name_detector_distinguishes_prose_from_ruled_coordinates(self) -> None:
        """Catches punctuation-bound retired prose without rejecting ruled coordinates."""

        for text in (RETIRED.capitalize(), RETIRED_SHORT + ",", RETIRED.upper() + "!"):
            with self.subTest(text=text):
                self.assertIsNotNone(RETIRED_PRODUCT_NAME.search(text))

        for text in (f"https://{RETIRED}.dev", f".{RETIRED}", f"x-{RETIRED}-*"):
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
                self.assertNotIn(f"scripts/{RETIRED_SHORT}", text)
                self.assertIsNone(RETIRED_PRODUCT_NAME.search(text))

    def test_public_living_docs_teach_floati_not_the_retired_name(self) -> None:
        """Catches living public prose or command examples left behind."""
        self.assert_docs_teach_floati_not_the_retired_name(PUBLIC_LIVING_DOCS)

    def test_tracked_tree_carries_no_retired_product_name(self) -> None:
        """The scope defect NAME-2 is about: this pattern scanned eleven files.

        A hand-written eleven-entry tuple sat in front of a tree with hundreds
        of hits and reported green, which is the most expensive shape a fence
        can have — it is not absent, so nobody adds one, and it is not looking,
        so nobody is warned. The population is now DERIVED, so the scanner
        cannot fall behind the tree by standing still.

        R-N4 Am.3 narrowed WHICH derivation: `git ls-files` bare put the fence
        in front of dated plans, specs and archived ruling requests that no
        export carries and that a standing ruling forbids rewriting. The
        population is the EXPORTER'S INCLUDE SET — the committed policy, read
        by the exporter's own classifier — so a policy change re-runs this
        question automatically and nobody maintains a second list.

        The ruled coordinates stay exempt through the pattern itself, not
        through a path list: `RETIRED_PRODUCT_NAME` already declines to match
        `.<name>` and `x-<name>-`, which are the on-disk names the product
        READS and must keep.
        """

        self.assertEqual(
            [], retired_name_offenders(REPOSITORY_ROOT, export_include_set())
        )

    def test_the_population_is_the_exporters_include_set_and_excludes_only_that(
        self,
    ) -> None:
        """Control: the narrowing is the policy's, and it is not the whole tree.

        A PROJECTION DETECTOR. In the published tree there is no policy and no
        exporter, and the tree IS the include set, so "narrower than the whole
        tree" is false there by construction -- it reported `1037 not less than
        1037` and was read as a defect. It skips as a typed absence instead.
        """

        require_private_artifact(self, POLICY_RELATIVE)
        tracked = _tracked_text_files()
        population = export_include_set()

        self.assertLess(len(population), len(tracked))
        # Am.4: the population must carry every ALREADY-PUBLISHED document that
        # sits under a now-private prefix. Leaving `public_paths` empty dropped
        # 206 of them and the fences never walked a single one. This is the
        # assertion that was missing, and it is the one that goes red without
        # the recorded baseline.
        published_under_private_prefixes = sorted(
            path
            for path in published_baseline()
            if path in set(tracked)
            and any(
                path.startswith(prefix)
                for prefix in export_policy().new_private_prefixes
            )
        )
        self.assertTrue(published_under_private_prefixes)
        # A published document may still leave the export, but only by an
        # EXPLICIT rule -- `private_only_paths` or a `class3_prefixes` entry,
        # which is how NAME-2d withdrew the two captures. Dropping one for
        # `new_private_prefix:` is the defect: that rule is the one the baseline
        # overrides, and reaching it means the baseline was not consulted.
        exporter = importlib.import_module("scripts.export_public")
        policy = export_policy()
        missing = sorted(set(published_under_private_prefixes) - set(population))
        self.assertEqual(
            [],
            [
                path
                for path in missing
                if not exporter.classify_path(path, published_baseline(), policy)
                .rule.startswith(("private_only_exact", "class3_prefix:"))
            ],
        )
        self.assertEqual(list(population), [p for p in tracked if p in set(population)])
        # Every path the narrowing dropped is dropped BY THE POLICY, not by a
        # name: re-classify the complement and require an explicit exclusion.
        self.assertEqual((), classify_inventory(set(tracked) - set(population)))

    def test_a_planted_hit_in_an_included_path_is_a_finding(self) -> None:
        """Control (a): the fence still sees the files an export would carry."""

        require_private_artifact(self, POLICY_RELATIVE)
        relative = "floati/name_fence_control_fixture.py"
        self.assertEqual((relative,), classify_inventory((relative,)))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            planted = root / relative
            planted.parent.mkdir(parents=True, exist_ok=True)
            planted.write_text(f"DOMAIN = '{RETIRED}'\n", encoding="utf-8")

            self.assertEqual(
                [f"{relative}:1"],
                retired_name_offenders(root, classify_inventory((relative,))),
            )

    def test_the_same_hit_in_an_excluded_path_is_not_a_finding(self) -> None:
        """Control (b): the narrowing, proved on the same bytes at another path.

        A PROJECTION DETECTOR for the same reason as the population control:
        with no policy there is no "excluded", so the excluded path classifies
        as included and the control cannot pose its question.
        """

        require_private_artifact(self, POLICY_RELATIVE)
        relative = "docs/superpowers/name-fence-control-fixture.md"
        self.assertEqual((), classify_inventory((relative,)))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            planted = root / relative
            planted.parent.mkdir(parents=True, exist_ok=True)
            planted.write_text(f"DOMAIN = '{RETIRED}'\n", encoding="utf-8")

            # The bytes ARE a finding — the same scanner says so when the path
            # is supplied directly — and the population is what clears them.
            self.assertEqual([f"{relative}:1"], retired_name_offenders(root, (relative,)))
            self.assertEqual(
                [], retired_name_offenders(root, classify_inventory((relative,)))
            )

    def test_an_unclassifiable_path_stays_in_the_population(self) -> None:
        """Fail closed: a path no export rule places is scanned, not dropped."""

        require_private_artifact(self, POLICY_RELATIVE)
        relative = "unruled-new-directory/NOTES.md"
        self.assertEqual((relative,), classify_inventory((relative,)))

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
        self.assertNotIn(public_ids.builder(RETIRED), frame)


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

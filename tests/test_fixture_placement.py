"""A test that ships may not open a path the export excludes.

`tests/` is a product prefix and exports; `docs/evidence/` is private and does
not. A shipped test that reads a fixture under a private prefix passes in the
harbor and ERRORS in the projection -- the only tree a public reader can run --
and neither the push fence nor the harbor suite can see it. That is how three
tests reached `720a3550`.

**The classification is the exporter's own, never a hand list.** Every candidate
path goes through `tests.export_inventory.classify_inventory`, which loads
`.github/public-export-policy.v0.json` with `scripts/export_public.py`'s own
reader and consults the published baseline. When the policy is absent -- which
is exactly the projected tree, where the exporter and the policy are themselves
`private_only_paths` -- that helper is the identity, so this file ships and
passes rather than erroring on the artifacts it is about.

**What this fence CAN see:** a string literal that is an operand of a `/` path
join, or the first argument of `open`/`Path`/`PurePath`/`PurePosixPath`/
`joinpath`, whose first segment names a tracked top-level entry.

**What it CANNOT see**, each counted rather than assumed by
`test_the_fence_walks_a_real_population`:

* A path built at runtime -- an f-string, a variable, a formatted join. Those
  are counted as dynamic operands and are invisible to this fence.
* A join anchored on something that is not the repository -- `self.source /
  "tools/..."` writes into a synthetic tree under the temp root, and the same
  literal would be a false RED. Those are counted as unanchored and skipped, so
  a test that anchors a REAL repository read on an attribute escapes this fence.
* Whether a read is reached at runtime. A private read guarded by `skipTest`,
  `require_private_artifact`, or an `exists()`/`is_file()` branch skips in the
  projection instead of erroring; those are counted as guarded and allowed. The
  guard is recognised per enclosing function, so a guard for one path exempts
  every private path in the same function.
* Anything outside `tests/`. `floati/` and `scripts/` ship too; this fence is
  scoped to the population the ruling names.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from tests.export_inventory import (
    classify_inventory,
    export_policy_is_present,
    materialise_adapted_tree,
    tracked_files,
)
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPOSITORY_ROOT / "tests"

#: Calls whose first argument names a filesystem path.
_OPENERS = frozenset({"open", "Path", "PurePath", "PurePosixPath", "joinpath"})
#: A call that makes a read conditional rather than mandatory.
_GUARD_CALLS = frozenset({"skipTest", "require_private_artifact"})
#: A predicate an `if` uses to decide whether the artifact is here at all.
_GUARD_PREDICATES = frozenset({"exists", "is_file", "is_dir"})
#: A path the planted control must classify private. Nothing is written here.
_PLANTED_PRIVATE = "docs/evidence/planted-control-20260905/fixture.json"


@dataclass(frozen=True)
class PathSite:
    """One repository-path literal a test file names in a path context."""

    relative: str
    lineno: int
    value: str

    def __str__(self) -> str:
        return f"{self.relative}:{self.lineno} opens {self.value!r}"


@dataclass(frozen=True)
class Survey:
    """What the walk saw, so a fence that went blind cannot pass quietly."""

    files_walked: int
    files_skipped: int
    sites: tuple[PathSite, ...]
    unanchored: int
    guarded: int
    dynamic: int


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", "")


def _join_root(node: ast.AST) -> ast.AST | None:
    """The leftmost operand of a `/` chain."""

    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        node = node.left
    return node


def _anchored_on_repository(root: ast.AST | None) -> bool:
    """A bare `Path("...")`/`open("...")` is repository-relative; the suite runs
    from the repository root. A join is only repository-anchored when its root
    is a plain name or a call -- `REPOSITORY_ROOT / ...`, `Path(__file__)
    .parents[1] / ...`. An attribute or subscript root (`self.source / ...`) is
    some other tree and is not this fence's business."""

    return root is None or isinstance(root, (ast.Name, ast.Call))


def _guarded(function: ast.AST) -> bool:
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and _call_name(node) in _GUARD_CALLS:
            return True
        if isinstance(node, ast.If):
            for inner in ast.walk(node.test):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr in _GUARD_PREDICATES
                ):
                    return True
    return False


def _walk(tree: ast.AST) -> tuple[list[tuple[int, str, ast.AST | None, ast.AST | None]], int]:
    """Return `(literal, join root, enclosing function)` triples and a dynamic count."""

    found: list[tuple[int, str, ast.AST | None, ast.AST | None]] = []
    dynamic = 0

    def literal_or_dynamic(node: ast.AST, root: ast.AST | None, function: ast.AST | None) -> None:
        nonlocal dynamic
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.lineno, node.value, root, function))
        elif isinstance(node, ast.JoinedStr):
            dynamic += 1

    def descend(node: ast.AST, function: ast.AST | None) -> None:
        nonlocal dynamic
        for child in ast.iter_child_nodes(node):
            inner = (
                child
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else function
            )
            if isinstance(child, ast.BinOp) and isinstance(child.op, ast.Div):
                root = _join_root(child.left)
                literal_or_dynamic(child.left, root, inner)
                literal_or_dynamic(child.right, root, inner)
            elif isinstance(child, ast.Call) and child.args:
                if _call_name(child) in _OPENERS:
                    base = (
                        child.func.value
                        if isinstance(child.func, ast.Attribute)
                        else None
                    )
                    literal_or_dynamic(child.args[0], base, inner)
            descend(child, inner)

    descend(tree, None)
    return found, dynamic


def survey(root: Path = REPOSITORY_ROOT) -> Survey:
    """Walk every shipped `tests/**/*.py` and report every path site it names."""

    top_level = frozenset(
        relative.split("/")[0] for relative in tracked_files(root) if "/" in relative
    )
    sources = sorted((root / "tests").rglob("*.py"))
    relatives = tuple(source.relative_to(root).as_posix() for source in sources)
    shipped = frozenset(classify_inventory(relatives, root=root))

    sites: list[PathSite] = []
    walked = unanchored = guarded = dynamic = 0
    for source, relative in zip(sources, relatives):
        if relative not in shipped:
            continue
        walked += 1
        found, module_dynamic = _walk(ast.parse(source.read_text(encoding="utf-8")))
        dynamic += module_dynamic
        for lineno, value, join_root, function in found:
            if not value or value.startswith("/") or "/" not in value:
                continue
            if value.split("/")[0] not in top_level:
                continue
            if not _anchored_on_repository(join_root):
                unanchored += 1
                continue
            if function is not None and _guarded(function):
                guarded += 1
                continue
            sites.append(PathSite(relative, lineno, value))

    return Survey(
        files_walked=walked,
        files_skipped=len(relatives) - walked,
        sites=tuple(sites),
        unanchored=unanchored,
        guarded=guarded,
        dynamic=dynamic,
    )


def _private(sites: tuple[PathSite, ...], root: Path = REPOSITORY_ROOT) -> tuple[PathSite, ...]:
    """The sites whose path the exporter's own classifier excludes.

    A literal naming a directory is classified with its trailing slash, because
    every policy prefix is slash-terminated and a directory only ever matters
    through the files beneath it: `docs/evidence/captures` is the class-1
    evidence prefix and exports, while `docs/evidence/captures/x` under a
    trailing-slash-free comparison would read as private.
    """

    candidates = []
    for site in sites:
        candidate = site.value.rstrip("/")
        if (root / candidate).is_dir():
            candidate += "/"
        candidates.append(candidate)
    kept = set(classify_inventory(candidates, root=root))
    return tuple(
        site for site, candidate in zip(sites, candidates) if candidate not in kept
    )


class FixturePlacementTests(unittest.TestCase):
    def test_no_shipped_test_opens_a_privately_classified_path(self) -> None:
        """The projected suite errors on a fixture the export leaves behind."""

        offenders = _private(survey().sites)

        self.assertEqual(
            [],
            [str(site) for site in offenders],
            "a test under tests/ ships and its fixture does not; move the "
            "fixture to tests/fixtures/<row>/ and let the evidence document "
            "cite it there",
        )

    def test_the_path_census_predicts_a_real_projection(self) -> None:
        """PROJ-LEG-2: the census itself is a test that has to survive shipping.

        Mirrors ``test_h1_f1.test_the_pin_predicts_a_real_projection``:
        build one real projection of the included set, ``git init`` it,
        and run this module inside it. The fence walks tests that SHIP,
        so the fence module is under its own rule twice over — it must be
        green where the export leaves the private artifacts behind, and
        it must collect there at all. A census module that imports or
        reads something the export excludes is silent here and broken in
        the only tree a public reader can run.

        PROJ-LEG-3: the projection is built through the exporter's own
        adaptation, not raw copies — the fence reads test SOURCES, and
        the sources a reader has are the adapted ones, so the census
        must walk what exposure actually wrote, literals and all.
        """

        if not export_policy_is_present():
            self.skipTest("no export policy in this tree; classification is identity")

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            projection = Path(temporary) / "projection"
            projection.mkdir()
            materialise_adapted_tree(projection, root=REPOSITORY_ROOT)

            def git(*arguments: str) -> None:
                environment = {
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("GIT_")
                }
                subprocess.run(
                    ["/usr/bin/git", *arguments],
                    cwd=projection,
                    env=environment,
                    check=True,
                    capture_output=True,
                )

            git("init", "-q", "--initial-branch=main")
            git("config", "user.name", "fixture")
            git("config", "user.email", "fixture@example.invalid")
            git("add", ".")
            git("commit", "-q", "-m", "projection fixture")

            completed = subprocess.run(
                [sys.executable, "-m", "unittest", "tests.test_fixture_placement"],
                cwd=projection,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True,
                text=True,
            )

        self.assertEqual(
            0,
            completed.returncode,
            "the projected path census failed inside the projection:\n"
            + completed.stderr[-4000:],
        )

    def test_the_fence_walks_a_real_population(self) -> None:
        """A broken walk sees nothing and passes; these are its zeros."""

        measured = survey()

        self.assertGreaterEqual(measured.files_walked, 250, measured)
        self.assertGreaterEqual(len(measured.sites), 200, measured)

        # Positive control: a path the walk must find, in a file it must walk.
        self.assertIn(
            ("tests/test_wake_hold.py", "schemas/v1/wake-attempt-record.schema.json"),
            {(site.relative, site.value) for site in measured.sites},
        )

        # Negative control: the classifier arm must call a planted private path
        # private. Nothing is written -- `classify_inventory` is a pure function
        # of path strings. It is skipped in the projection, where the policy is
        # absent by design and the helper is the identity.
        if not export_policy_is_present():
            self.skipTest("no export policy in this tree; classification is identity")
        planted = PathSite("tests/test_fixture_placement.py", 0, _PLANTED_PRIVATE)
        self.assertEqual((planted,), _private((planted,)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

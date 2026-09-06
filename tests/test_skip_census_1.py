"""SKIP-CENSUS-1: every skip reason the suite can emit is a named pin.

A skip is a test that did not run. A green suite with a new skip reason is
the same class of invisibility as a duplicated `def test_`: the count can
stay right while a question disappears. CI-RT-1c pinned one family that
never emitted; this fence pins the SET of reasons, by AST, plus the reasons
the AT box leg actually printed (run 33980338058, skipped=8).

A reason outside the set is a finding. Counts are AST sites (one helper
that two tests call is one site). Box-leg counts are observed skips.
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIRECTORY = REPOSITORY_ROOT / "tests"
HOST_CAPABILITY = "host-capability"
PRODUCT = "product"
KIND_VALUES = frozenset({HOST_CAPABILITY, PRODUCT})
AT_BOX_LEG_RUN = "33980338058"
AT_BOX_LEG_SKIPPED = 8
PINNED_BOX_LEG_RUN = AT_BOX_LEG_RUN

# AST site counts. A helper two tests call is one site; the box-leg pin
# records how many tests actually skipped under run 33980338058.
PINNED_SKIP_REASONS = {
    # FIXTURE-1's fence (train AV) skips by design in a projection, where the
    # export policy is absent and classification is the identity. H1-F1 Am.2's
    # projection-builder leg words the same skip: it cannot build a projection
    # without a policy either.
    "no export policy in this tree; classification is identity": {
        "sites": 2,
        "kind": PRODUCT,
    },
    # NET-FENCE-1-F2's private_only twins (tests/test_no_listener_fence.py,
    # tests/test_h1_f1.py) skip by design in a projection: the excluded half
    # they pin exists only in the harbor, where the export policy lives.
    "export_policy_absent: a policy-less projection carries no excluded half, so the private half is a typed skip": {
        "sites": 2,
        "kind": PRODUCT,
    },
    # BASELINE-1-F1 Am.1 (7e1aa486) runs one test as a child of a
    # projection-shape run and skips it there by design; pinned on the AZ
    # union where the car first met this census.
    "child of a projection-shape run; not this test's subject": {
        "sites": 1,
        "kind": PRODUCT,
    },
    "Pillow is not installed": {"sites": 7, "kind": HOST_CAPABILITY},
    "macOS descriptor surface": {"sites": 3, "kind": HOST_CAPABILITY},
    "Claude reference adapter is unavailable": {
        "sites": 1,
        "kind": HOST_CAPABILITY,
    },
    "FIFO fixtures are unavailable on this host": {
        "sites": 1,
        "kind": HOST_CAPABILITY,
    },
    # ROLE-1's role-library FIFO gate (tests/test_role_library.py) words the
    # same host-capability skip differently; it rode the composition after
    # this census's base and is pinned here under its own string.
    "FIFO fixture requires local FIFO support": {
        "sites": 1,
        "kind": HOST_CAPABILITY,
    },
    "Linux live systemd activate is row 20, not this sitting": {
        "sites": 1,
        "kind": PRODUCT,
    },
    "Linux live systemd is the LX-phase conformance row": {
        "sites": 1,
        "kind": PRODUCT,
    },
    "conforming Draft 2020-12 validator is unavailable": {
        "sites": 1,
        "kind": HOST_CAPABILITY,
    },
    "floati.mcp_pin is not implemented": {"sites": 1, "kind": PRODUCT},
    "harbor-only history contract": {"sites": 1, "kind": PRODUCT},
    "not in this tree (private to the harbor repository by export policy): {dyn}": {
        "sites": 1,
        "kind": PRODUCT,
    },
    "no canonical sticky temp root owned by another uid on this host": {
        "sites": 1,
        "kind": HOST_CAPABILITY,
    },
    "no runnable non-root-owned Python interpreter": {
        "sites": 1,
        "kind": HOST_CAPABILITY,
    },
    "optional jsonschema standards probe is unavailable": {
        "sites": 1,
        "kind": HOST_CAPABILITY,
    },
    "reference adapter unavailable": {"sites": 1, "kind": HOST_CAPABILITY},
    "symlinks unavailable": {"sites": 1, "kind": HOST_CAPABILITY},
    "this host does not expose both tmp directory spellings": {
        "sites": 1,
        "kind": HOST_CAPABILITY,
    },
    "this host does not reach the same temp directory through a tmp symlink and a real path": {
        "sites": 1,
        "kind": HOST_CAPABILITY,
    },
    "this host's default TMPDIR has no symlinked ancestor; the Darwin mkdtemp refusal is unobservable here": {
        "sites": 1,
        "kind": HOST_CAPABILITY,
    },
}

# linux-selftest job of run 33980338058: FAILED (failures=3, skipped=8)
PINNED_BOX_LEG_SKIP_REASONS = {
    "macOS descriptor surface": 3,
    "this host does not expose both tmp directory spellings": 2,
    "Linux live systemd activate is row 20, not this sitting": 1,
    "Linux live systemd is the LX-phase conformance row": 1,
    "this host's default TMPDIR has no symlinked ancestor; the Darwin mkdtemp refusal is unobservable here": 1,
}

SKIP_DECORATORS = frozenset({"skip", "skipIf", "skipUnless", "expectedFailure"})


def test_modules(root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in root.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    )


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _reason_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{dyn}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _reason_text(node.left)
        right = _reason_text(node.right)
        if left is None and right is None:
            return None
        return (left or "{dyn}") + (right or "{dyn}")
    if isinstance(node, ast.Call):
        return "{dyn}"
    return None


def _decorator_reason(decorator: ast.AST) -> tuple[str, str | None] | None:
    target = decorator
    args: list[ast.AST] = []
    if isinstance(decorator, ast.Call):
        target = decorator.func
        args = list(decorator.args)
    name = _call_name(target)
    if name not in SKIP_DECORATORS:
        return None
    if name == "skip":
        reason = _reason_text(args[0]) if args else None
    elif name in {"skipIf", "skipUnless"}:
        reason = _reason_text(args[1]) if len(args) >= 2 else None
    else:
        reason = _reason_text(args[0]) if args else "(expectedFailure)"
    return name, reason


def skip_reason_sites(root: Path) -> list[tuple[str, str, str]]:
    """Return `(file, form, reason)` for every skip the suite can emit.

    Direct AST members only. A `skipIf` living inside a string fixture is
    not a skip the loader can take.
    """

    sites: list[tuple[str, str, str]] = []
    for path in test_modules(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:
            sites.append((path.name, "unparseable", error.msg))
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for decorator in node.decorator_list:
                    parsed = _decorator_reason(decorator)
                    if parsed is None:
                        continue
                    form, reason = parsed
                    if reason is None:
                        sites.append((path.name, form, "<unresolved>"))
                    else:
                        sites.append((path.name, form, reason))
            if isinstance(node, ast.Call) and _call_name(node.func) == "skipTest":
                reason = _reason_text(node.args[0]) if node.args else None
                if reason is None:
                    sites.append((path.name, "skipTest", "<unresolved>"))
                else:
                    sites.append((path.name, "skipTest", reason))
    return sites


def skip_reason_counts(root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _path, _form, reason in skip_reason_sites(root):
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


class SkipCensusInstrumentTests(unittest.TestCase):
    """The walker, proved on fixtures before the pin exists."""

    def _fixture(self, stack, **modules: str) -> Path:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for name, source in modules.items():
            (root / f"{name}.py").write_text(source, encoding="utf-8")
        return root

    def test_skipunless_skipif_skiptest_and_expected_failure_are_all_found(self) -> None:
        from contextlib import ExitStack

        source = (
            "import unittest\n"
            "import sys\n"
            "\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    @unittest.skipUnless(sys.platform == 'darwin', 'macOS descriptor surface')\n"
            "    def test_darwin(self) -> None:\n"
            "        pass\n"
            "\n"
            "    @unittest.skipIf(False, 'floati.mcp_pin is not implemented')\n"
            "    def test_pin(self) -> None:\n"
            "        pass\n"
            "\n"
            "    @unittest.expectedFailure\n"
            "    def test_expected(self) -> None:\n"
            "        pass\n"
            "\n"
            "    def test_host(self) -> None:\n"
            "        self.skipTest('FIFO fixtures are unavailable on this host')\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_skips=source)
            self.assertEqual(
                {
                    "FIFO fixtures are unavailable on this host": 1,
                    "(expectedFailure)": 1,
                    "floati.mcp_pin is not implemented": 1,
                    "macOS descriptor surface": 1,
                },
                skip_reason_counts(root),
            )

    def test_a_string_fixture_is_not_a_skip_the_loader_can_take(self) -> None:
        from contextlib import ExitStack

        source = (
            "import unittest\n"
            "\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_one(self) -> None:\n"
            "        snippet = '@unittest.skipIf(False, \"never\")\\n'\n"
            "        self.assertIn('skipIf', snippet)\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_string=source)
            self.assertEqual({}, skip_reason_counts(root))


class SkipCensusPopulationTests(unittest.TestCase):
    def test_the_private_artifact_skip_is_in_the_census(self) -> None:
        """RED: glob test_*.py cannot see tests/private_artifacts.py."""

        files = {path for path, _form, _reason in skip_reason_sites(TESTS_DIRECTORY)}
        self.assertIn("private_artifacts.py", files)

    def test_a_binop_skip_reason_keeps_the_static_prefix_and_types_the_tail(self) -> None:
        """RED: a Concat skipTest reason is unresolved. GREEN: prefix + {dyn}."""

        from contextlib import ExitStack

        source = (
            "import unittest\n"
            "\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_export(self) -> None:\n"
            "        missing = ['policy.json']\n"
            "        self.skipTest('not in this tree: ' + ', '.join(missing))\n"
        )
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            (root / "helper.py").write_text(source, encoding="utf-8")
            self.assertEqual({"not in this tree: {dyn}": 1}, skip_reason_counts(root))


class SkipCensusPinTests(unittest.TestCase):
    def test_skip_reason_set_is_pinned(self) -> None:
        """RED: no pin exists. GREEN: the AST census equals the pin."""

        pin = globals().get("PINNED_SKIP_REASONS")
        self.assertIsNotNone(pin, "skip_reason_set_drift: no pin exists")
        self.assertTrue(pin)
        for reason, spec in pin.items():
            with self.subTest(reason=reason):
                self.assertIn(spec["kind"], KIND_VALUES)
                self.assertGreaterEqual(spec["sites"], 1)
        observed = skip_reason_counts(TESTS_DIRECTORY)
        expected = {reason: spec["sites"] for reason, spec in pin.items()}
        extra = sorted(set(observed) - set(expected))
        missing = sorted(set(expected) - set(observed))
        self.assertEqual(
            [],
            extra,
            "skip_reason_set_drift: reasons outside the pin: " + ", ".join(extra),
        )
        self.assertEqual(
            [],
            missing,
            "skip_reason_set_drift: pinned reasons the AST no longer emits: "
            + ", ".join(missing),
        )
        self.assertEqual(expected, observed)

    def test_at_box_leg_skip_reasons_are_pinned(self) -> None:
        """RED: the AT box-leg observed set is unnamed. GREEN: skipped=8 is pinned."""

        pin = globals().get("PINNED_BOX_LEG_SKIP_REASONS")
        self.assertIsNotNone(pin, "skip_reason_set_drift: no box-leg pin exists")
        self.assertEqual(AT_BOX_LEG_SKIPPED, sum(pin.values()))
        self.assertEqual(AT_BOX_LEG_RUN, globals().get("PINNED_BOX_LEG_RUN"))
        declared = set(PINNED_SKIP_REASONS)
        outside = sorted(set(pin) - declared)
        self.assertEqual(
            [],
            outside,
            "skip_reason_set_drift: box-leg reasons outside the AST pin: "
            + ", ".join(outside),
        )

    def test_a_reason_outside_the_pin_is_a_finding(self) -> None:
        from contextlib import ExitStack

        source = (
            "import unittest\n"
            "\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_new(self) -> None:\n"
            "        self.skipTest('brand-new skip reason')\n"
        )
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            (root / "test_new.py").write_text(source, encoding="utf-8")
            extra = sorted(set(skip_reason_counts(root)) - set(PINNED_SKIP_REASONS))
            self.assertEqual(["brand-new skip reason"], extra)


if __name__ == "__main__":
    unittest.main()

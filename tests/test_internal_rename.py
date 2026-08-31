from __future__ import annotations

import ast
import importlib
import json
import os
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FLOATI_SCHEMA_ORIGIN = "https://landoclusters.com/floati/schemas/"
INTENTIONALLY_UNIDENTIFIED_SCHEMAS = frozenset(
    {
        "schemas/v1/compensation-executed-record.schema.json",
        "schemas/v1/compensation-proposed-record.schema.json",
        "schemas/v1/effect-failed-record.schema.json",
        "schemas/v1/effect-reconciled-record.schema.json",
        "schemas/v1/effect-status-artifact.schema.json",
        "schemas/v1/effect-unknown-record.schema.json",
        "schemas/v1/thread-observation-status-artifact.schema.json",
        "schemas/v1/wake-decision-artifact.schema.json",
        "schemas/v1/wake-hold-receipt-record.schema.json",
        "schemas/v1/wake-path-status-artifact.schema.json",
    }
)
FROZEN_IDENTITY_FORBIDDEN_TOKENS = (
    "slipway.dev",
    "slipway.local",
    "floati.dev",
    "floati.local",
    "Slipway",
    "x-slipway-",
    "\x2fprivate\x2ftmp/slipway-work",
    r"com\\.slipway\\.oneshot\\.",
    '"path": "slip/',
    '"path": "scripts/slip"',
)
_LEGACY_MODULE_ROOT = "sl" + "ip"
LEGACY_MODULE_NAMESPACE = re.compile(
    rf"""
    (?x)
    (?:
        (?<![A-Za-z0-9_]){re.escape(_LEGACY_MODULE_ROOT)}\.[A-Za-z_][A-Za-z0-9_]*
        |
        \b(?:
            from\s+{re.escape(_LEGACY_MODULE_ROOT)}(?:\s+import\b|\.)
            |
            import\s+{re.escape(_LEGACY_MODULE_ROOT)}(?:\s|$|,|\.)
        )
        |
        \bpython(?:3)?\s+-m\s+{re.escape(_LEGACY_MODULE_ROOT)}
        (?:\.[A-Za-z_][A-Za-z0-9_]*)?(?=$|\s|[\"'])
    )
    """
)


def _legacy_module_namespace_reference(source: str) -> str | None:
    """Return a legacy module coordinate, never ordinary frozen prose."""

    text_match = LEGACY_MODULE_NAMESPACE.search(source)
    if text_match is not None:
        return text_match.group(0)

    try:
        module = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_legacy_module_name(alias.name):
                    return alias.name
        if isinstance(node, ast.ImportFrom) and _is_legacy_module_name(node.module):
            return node.module
        if isinstance(node, (ast.List, ast.Tuple)):
            for option, module_name in zip(node.elts, node.elts[1:]):
                if _string_literal(option) == "-m" and _is_legacy_module_name(
                    _string_literal(module_name)
                ):
                    return _string_literal(module_name)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            if any(_is_package_target(target) for target in targets) and _is_legacy_module_name(
                _string_literal(node.value)
            ):
                return _string_literal(node.value)
        if isinstance(node, ast.Subscript) and _is_sys_modules(node.value):
            module_name = _string_literal(node.slice)
            if _is_legacy_module_name(module_name):
                return module_name
        if isinstance(node, ast.Call) and _call_name(node.func) in {
            "ModuleType",
            "__import__",
            "import_module",
        }:
            if node.args:
                module_name = _string_literal(node.args[0])
                if _is_legacy_module_name(module_name):
                    return module_name
    return None


def _is_legacy_module_name(name: str | None) -> bool:
    return name == _LEGACY_MODULE_ROOT or (
        name is not None and name.startswith(_LEGACY_MODULE_ROOT + ".")
    )


def _string_literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_package_target(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Name) and node.id == "__package__"
    ) or (
        isinstance(node, ast.Attribute) and node.attr == "__package__"
    )


def _is_sys_modules(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "modules"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _active_module_identity_paths() -> list[Path]:
    completed = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--",
            "floati",
            "tests",
            "scripts",
            ".github/workflows",
            "Makefile",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths: list[Path] = []
    for raw_relative in completed.stdout.split(b"\0"):
        if not raw_relative:
            continue
        relative = Path(raw_relative.decode("utf-8"))
        if relative == Path("Makefile"):
            paths.append(ROOT / relative)
        elif relative.parts[:2] == (".github", "workflows"):
            if relative.suffix in {".yml", ".yaml"}:
                paths.append(ROOT / relative)
        elif relative.parts[0] == "scripts":
            paths.append(ROOT / relative)
        elif relative.parts[0] in {"floati", "tests"} and relative.suffix == ".py":
            paths.append(ROOT / relative)
    return sorted(paths)


def _frozen_schema_paths() -> list[Path]:
    return (
        sorted((ROOT / "schemas").glob("v*/*.json"))
        + sorted((ROOT / "bundle" / "c7.1" / "schemas").glob("*.json"))
        + sorted((ROOT / "bundle" / "c7.2" / "schemas").glob("*.json"))
    )


def _deployable_frozen_identity_hits() -> list[tuple[str, str]]:
    deployable = [ROOT / "bundle-manifest.v0.json"]
    deployable.extend(sorted((ROOT / "schemas").glob("v*/*.json")))
    deployable.extend(sorted((ROOT / "bundle" / "c7.1").glob("**/*")))
    deployable.extend(sorted((ROOT / "bundle" / "c7.2").glob("**/*")))
    hits: list[tuple[str, str]] = []
    for path in deployable:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for forbidden in FROZEN_IDENTITY_FORBIDDEN_TOKENS:
            if forbidden in text:
                hits.append((path.relative_to(ROOT).as_posix(), forbidden))
    return hits


class InternalRenameCodeIdentityTests(unittest.TestCase):
    def test_deployable_frozen_assets_use_only_floati_identity(self) -> None:
        """Catches a deployable frozen byte that retains a retired or unowned coordinate."""

        self.maxDiff = None
        self.assertEqual([], _deployable_frozen_identity_hits())

    def test_schema_ids_use_owned_floati_origin_without_assigning_missing_ids(self) -> None:
        """Catches re-baselining to an unowned origin or inventing an identity for an anonymous schema."""

        missing: set[str] = set()
        for path in _frozen_schema_paths():
            schema = json.loads(path.read_text(encoding="utf-8"))
            relative = path.relative_to(ROOT).as_posix()
            if "$id" not in schema:
                missing.add(relative)
                continue
            with self.subTest(schema=relative):
                self.assertIsInstance(schema["$id"], str)
                self.assertTrue(
                    schema["$id"].startswith(FLOATI_SCHEMA_ORIGIN),
                    schema["$id"],
                )
        self.assertEqual(INTENTIONALLY_UNIDENTIFIED_SCHEMAS, frozenset(missing))

    def test_legacy_module_namespace_detector_rejects_coordinates_not_frozen_prose(self) -> None:
        """Catches a retired import coordinate without rejecting frozen Class 3 copy."""

        legacy = _LEGACY_MODULE_ROOT
        for source in (
            f'target = "{legacy}.worker_exec"',
            f"sys.modules[{legacy!r}] = module",
            f"import {legacy}",
            f"from {legacy} import root",
            f'script = """from {legacy} import root\npython3 -m {legacy}.selftest"""',
            f'command = ["python3", "-m", "{legacy}", "status"]',
        ):
            with self.subTest(source=source):
                self.assertIsNotNone(_legacy_module_namespace_reference(source))

        for source in (
            'reason = "A slip ahead of the installed copy answered first on PATH."',
            "slip_source_path = source_path",
        ):
            with self.subTest(source=source):
                self.assertIsNone(_legacy_module_namespace_reference(source))

    def test_floati_package_is_the_only_python_identity(self) -> None:
        self.assertTrue((ROOT / "floati" / "__init__.py").is_file())
        for legacy_path in (
            ROOT / _LEGACY_MODULE_ROOT,
            ROOT / "scripts" / _LEGACY_MODULE_ROOT,
        ):
            with self.subTest(legacy_path=legacy_path):
                self.assertFalse(os.path.lexists(legacy_path))

        errors = importlib.import_module("floati.errors")
        roots = importlib.import_module("floati.root")
        self.assertTrue(hasattr(errors, "FloatiError"))
        self.assertFalse(hasattr(errors, "SlipwayError"))
        self.assertTrue(hasattr(roots, "FloatiRoot"))
        self.assertFalse(hasattr(roots, "SlipRoot"))

    def test_floati_launcher_resolves_only_the_floati_package(self) -> None:
        launcher_path = ROOT / "scripts" / "floati"
        self.assertTrue(launcher_path.is_file())
        completed = subprocess.run(
            [str(launcher_path), "--help"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        executable_lines = [
            line.strip()
            for line in launcher_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertTrue(executable_lines, "floati launcher must have an executable line")
        self.assertEqual('exec python3 -m floati "$@"', executable_lines[-1])

    def test_dynamic_runtime_modules_resolve_under_floati(self) -> None:
        floati_root = ROOT / "floati"
        self.assertTrue(
            (floati_root / "__init__.py").is_file(),
            "floati package must exist before dynamic modules can resolve",
        )

        resolved_floati_root = floati_root.resolve()
        self.assertEqual(ROOT.resolve() / "floati", resolved_floati_root)
        source_paths = _active_module_identity_paths()
        self.assertTrue(source_paths, "active module identity surfaces must be tracked")
        for source_path in source_paths:
            source = source_path.read_text(encoding="utf-8")
            with self.subTest(source_path=source_path.relative_to(ROOT)):
                self.assertIsNone(
                    _legacy_module_namespace_reference(source),
                    f"legacy module namespace remains in {source_path.relative_to(ROOT)}",
                )

        for name in (
            "floati.worker_bootstrap",
            "floati.worker_exec",
            "floati.effect_reconciliation_exec",
            "floati.adapters.codex_live",
        ):
            with self.subTest(name=name):
                module = importlib.import_module(name)
                self.assertEqual(name, module.__name__)
                spec = getattr(module, "__spec__", None)
                self.assertIsNotNone(spec)
                self.assertEqual(name, spec.name)
                module_file = getattr(module, "__file__", None)
                self.assertIsInstance(module_file, str)
                module_path = Path(module_file)
                self.assertTrue(module_path.is_file())
                resolved_module_path = module_path.resolve()
                try:
                    resolved_module_path.relative_to(resolved_floati_root)
                except ValueError:
                    self.fail(
                        f"{name} resolves outside {resolved_floati_root}: "
                        f"{resolved_module_path}"
                    )


if __name__ == "__main__":
    unittest.main()

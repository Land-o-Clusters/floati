"""F10-1 gate one — STATIC IMPORT CLOSURE over the shipped bundle.

Ruling (`docs/design/f10-1-remedy-ruling-2026-08-30.md`): parse every
shipped module's imports, resolve them to paths, and assert
**closure ⊆ manifest membership**. Derivable, fast, executes nothing,
and catches this entire class rather than this one instance.

★ A MANIFEST THAT DERIVES ITS OWN SET CANNOT DETECT A FILE THE
DERIVATION EXCLUDES — so the manifest is NOT asked to police itself:
this bank reads the manifest's file list and the DEPLOYABLE PATTERN SET
(`floati/**/*.py`) and checks every module the patterns ship, so a
dark file excluded from the manifest is still caught when a SHIPPED
module imports it.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from floati.manifest import _deployable_paths

REPOSITORY_ROOT = Path(__file__).parents[1]


def _module_path(name: str) -> Path | None:
    """Resolve a dotted module name to a file under floati/, if it is one."""
    if not name or not name.split(".")[0] == "floati":
        return None
    parts = name.split(".")[1:]
    base = REPOSITORY_ROOT / "floati"
    for candidate in (
        base.joinpath(*parts).with_suffix(".py"),
        base.joinpath(*parts, "__init__.py"),
    ):
        if candidate.is_file():
            return candidate
    return None


def _imported_module_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module)
            elif node.level > 0:
                # relative import: the module's own package prefix is
                # resolved by the caller, which passes the file's name
                names.add(("." * node.level) + (node.module or ""))
    return names


def _resolve(name: str, source_file: Path) -> Path | None:
    if name.startswith("."):
        package_parts = source_file.relative_to(
            REPOSITORY_ROOT).with_suffix("").parts[1:-1]
        if source_file.name != "__init__.py":
            package_parts = package_parts[:-1]
        trimmed = name.lstrip(".")
        levels = len(name) - len(trimmed)
        parts = list(package_parts[: len(package_parts) - (levels - 1)])\
            if levels > 1 else list(package_parts)
        if trimmed:
            parts.extend(trimmed.split("."))
        dotted = ".".join(("floati", *parts))
        return _module_path(dotted)
    return _module_path(name)


class ImportClosureTests(unittest.TestCase):
    def test_every_shipped_module_closes_inside_the_manifest(self) -> None:
        """One law: a live module may not import a file the release cannot
        vouch for. The manifest is the release's own vouching set; the
        deployable patterns are the shipping set; closure is checked over
        the shipping set so an excluded dark file cannot hide behind its
        own exclusion."""
        manifest_paths = set(_deployable_paths(REPOSITORY_ROOT))
        manifest_names = {
            path[:-3].replace("/", ".")
            for path in manifest_paths
            if path.startswith("floati/") and path.endswith(".py")
        }
        offenders: dict[str, set[str]] = {}
        for relative in sorted(manifest_paths):
            if not (relative.startswith("floati/") and relative.endswith(".py")):
                continue
            source = REPOSITORY_ROOT / relative
            tree = ast.parse(source.read_text(encoding="utf-8"))
            module_name = relative[:-3].replace("/", ".")
            for name in _imported_module_names(tree):
                resolved = _resolve(name, source)
                if resolved is None:
                    continue  # stdlib or third-party: not bundle content
                resolved_relative = resolved.relative_to(REPOSITORY_ROOT).as_posix()
                if resolved_relative in manifest_paths:
                    continue
                resolved_name = resolved_relative[:-3].replace("/", ".")
                if resolved_name in manifest_names:
                    continue  # a package __init__ shipped by another listing
                offenders.setdefault(module_name, set()).add(resolved_relative)
        self.assertEqual(
            {}, offenders,
            "live modules import files the release cannot vouch for",
        )


if __name__ == "__main__":
    unittest.main()

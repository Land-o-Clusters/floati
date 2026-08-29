"""Discovery adapter for the landed night-watch draft contract tests."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


_DRAFT_ROOT = Path(__file__).parents[1] / "drafts" / "night-watch"
_TEST_PATHS = (
    _DRAFT_ROOT / "tests" / "test_fence.py",
    _DRAFT_ROOT / "tests" / "test_night_watch_scenarios.py",
)


def _load_module(path: Path, suffix: str):
    spec = importlib.util.spec_from_file_location(
        f"floati_draft_night_watch_{suffix}", path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load draft test module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_DRAFT_ROOT))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


_MODULES = tuple(
    _load_module(path, str(index)) for index, path in enumerate(_TEST_PATHS)
)


def _suite(loader: unittest.TestLoader) -> unittest.TestSuite:
    return unittest.TestSuite(loader.loadTestsFromModule(module) for module in _MODULES)


def _ids(suite: unittest.TestSuite) -> tuple[str, ...]:
    values = []
    stack = [suite]
    while stack:
        item = stack.pop()
        if isinstance(item, unittest.TestSuite):
            stack.extend(reversed(list(item)))
        else:
            values.append(item.id())
    return tuple(values)


DRAFT_TEST_IDS = _ids(_suite(unittest.defaultTestLoader))


def load_tests(loader, standard_tests, pattern):
    return _suite(loader)

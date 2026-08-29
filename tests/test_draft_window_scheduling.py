"""Discovery adapter for the landed window-scheduling draft contract tests."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


_DRAFT_ROOT = Path(__file__).parents[1] / "drafts" / "window-scheduling"
_TEST_PATH = _DRAFT_ROOT / "tests" / "test_window_scheduling.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "floati_draft_window_scheduling_tests", _TEST_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load draft test module: {_TEST_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_DRAFT_ROOT))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


_MODULE = _load_module()


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


DRAFT_TEST_IDS = _ids(unittest.defaultTestLoader.loadTestsFromModule(_MODULE))


def load_tests(loader, standard_tests, pattern):
    return loader.loadTestsFromModule(_MODULE)

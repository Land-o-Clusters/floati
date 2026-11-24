"""Bind native proof imports to this fixture bank without importing tests."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


PACKAGE_NAME = "_bus_scale_fixture_proofs"
PROOF_MODULES = (
    "bus_scale_prefix_effects", "bus_scale_receipt_prefix_effects",
    "bus_scale_cursor_prefix_effects", "bus_scale_inbox_prefix_effects",
    "bus_scale_consumed_prefix_effects", "bus_scale_registry_prefix_effects",
    "bus_scale_wake_hint_effects", "bus_scale_waiter_invocation_effects",
)


def bind():
    source = Path(__file__).resolve(strict=True)
    directory = str(source.parent)
    existing = sys.modules.get(PACKAGE_NAME)
    if existing is not None:
        specification = getattr(existing, "__spec__", None)
        if (getattr(existing, "__file__", None) != str(source)
                or getattr(existing, "__path__", None) != [directory]
                or specification is None or specification.name != PACKAGE_NAME
                or specification.origin != str(source)
                or getattr(existing, "__loader__", None) is not specification.loader
                or getattr(specification.loader, "path", None) != str(source)):
            raise RuntimeError("native fixture proof namespace belongs to another source")
        return existing
    specification = importlib.util.spec_from_file_location(
        PACKAGE_NAME, source, submodule_search_locations=[directory])
    module = importlib.util.module_from_spec(specification)
    sys.modules[PACKAGE_NAME] = module
    try:
        specification.loader.exec_module(module)
    except BaseException:
        if sys.modules.get(PACKAGE_NAME) is module:
            del sys.modules[PACKAGE_NAME]
        raise
    return module

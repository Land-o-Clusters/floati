"""Expose the same native proof modules through ordinary test imports."""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
import sys


_specification = importlib.util.spec_from_file_location(
    "_bus_scale_fixture_proof_bootstrap", Path(__file__).with_name("_proof_namespace.py"))
_bootstrap = importlib.util.module_from_spec(_specification)
_specification.loader.exec_module(_bootstrap)
_proofs = _bootstrap.bind()
for _name in _bootstrap.PROOF_MODULES:
    _module = importlib.import_module(_proofs.__name__ + "." + _name)
    sys.modules[__name__ + "." + _name] = _module
    globals()[_name] = _module

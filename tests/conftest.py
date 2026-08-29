"""FROZEN-RUNTIME LAW compliance (msg-01a029b7273d, final form).

A FROZEN TREE IS NOT A FROZEN RUNTIME: test sessions must never write
bytecode into the repository, so a gated Python package's frozen-runtime
equals its frozen-tree BY CONSTRUCTION — prevention independent of any
diagnosis of how stale bytecode once survived a restore.

This conftest is loaded before test modules are imported, so
``sys.dont_write_bytecode`` suppresses .pyc creation for every later
import in the session, and the environment variable covers subprocesses.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
sys.dont_write_bytecode = True

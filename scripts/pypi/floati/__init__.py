"""Expose the installed Floati runtime as a regular Python package."""
from importlib.metadata import version
from pathlib import Path

# Hooks and runtime readers use paths relative to the existing bundle layout.
# Keep that layout private to Floati instead of placing scripts, roles, and
# schemas at the top level of site-packages.
__path__ = [str(Path(__file__).parent / "_runtime" / "floati")]
__version__ = version("floati")

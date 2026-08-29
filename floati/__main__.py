"""Module entry point for ``python3 -m floati``."""

from __future__ import annotations

import sys

from .cli import main


sys.exit(main())

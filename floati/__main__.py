"""Module entry point for ``python3 -m floati``."""

from __future__ import annotations

import sys

if sys.argv[1:3] == ["hook", "relay"]:
    # The unchanged-inbox gate must run before importing the full CLI/runtime.
    from .claude_hook_relay import main
    sys.exit(main(sys.argv[3:]))

if sys.argv[1:3] == ["hook", "t3-dispatch"]:
    from .t3_dispatch import worker
    worker(sys.argv[3:])
    sys.exit(0)

from .cli import main


sys.exit(main())

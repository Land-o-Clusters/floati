#!/usr/bin/python3
"""Run the bounded headless-entry checks serially with a disposable HOME."""

import os
from pathlib import Path
import sys
import tempfile

from fixture_finalizer import install, owned_child


def main():
    root = Path(__file__).resolve().parents[1]
    install(temp_root="\x2fprivate/tmp")
    with tempfile.TemporaryDirectory(prefix="hw-home.", dir="\x2fprivate/tmp") as scratch_home:
        environment = dict(os.environ, HOME=scratch_home, PYTHONDONTWRITEBYTECODE="1")
        with owned_child(
            ["/usr/bin/python3", "-B", "-m", "unittest", *sys.argv[1:]],
            cwd=root, env=environment,
        ) as child:
            try:
                return child.wait()
            except KeyboardInterrupt:
                # The shared owner forwarded Ctrl-C to this exact child.
                # Keep the wrapper's historical child-exit return contract.
                return child.wait()


if __name__ == "__main__":
    sys.exit(main())

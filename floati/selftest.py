"""Run the complete Floati selftest without a result-masking wrapper."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from .manifest import EXPECTED_CANONICAL_REF, verify_manifest


TEST_FAILURE = 10
MANIFEST_MISMATCH = 34


def outcome_for(tests_successful: bool, manifest_errors: list) -> int:
    if not tests_successful:
        return TEST_FAILURE
    if manifest_errors:
        return MANIFEST_MISMATCH
    return 0


def emit_verified(stream) -> None:
    payload = {"canonical_ref": EXPECTED_CANONICAL_REF, "status": "bundle_verified"}
    if getattr(stream, "isatty", lambda: False)():
        from .brand import render_buoy_mark

        print(render_buoy_mark(color=True), file=stream)
    print(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ),
        file=stream,
    )


def main() -> int:
    suite = unittest.defaultTestLoader.discover("tests", pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return outcome_for(False, [])
    errors = verify_manifest(Path.cwd())
    if errors:
        print(
            json.dumps(
                {
                    "canonical_ref": EXPECTED_CANONICAL_REF,
                    "errors": errors,
                    "status": "bundle_mismatch",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return outcome_for(True, errors)
    emit_verified(sys.stdout)
    return outcome_for(True, [])


if __name__ == "__main__":
    sys.exit(main())

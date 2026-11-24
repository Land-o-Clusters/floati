from __future__ import annotations

from pathlib import Path
from unittest import TestCase


HARBOR_POLICY_RELATIVE = ".github/public-export-policy.v0.json"


def projected_role_text(value: str) -> str:
    """Return reviewed source text unchanged in every repository shape."""

    return value


def require_harbor_history(test: TestCase, repository: Path, *commits: str) -> None:
    """Skip source-history proofs only in the policy-less public tree."""
    if not (repository / HARBOR_POLICY_RELATIVE).is_file():
        test.skipTest("public_shallow_history: public shallow clone omits exact commit(s) "
                      + ", ".join(commits))

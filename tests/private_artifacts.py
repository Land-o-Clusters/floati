"""Skip, with a stated reason, when a test's subject is not in this tree.

The public projection deliberately strips files the harbor repository keeps —
the export policy, the codex gateway, the stop-hook bridge, the publication
checklist. Their tests shipped anyway and failed, so the artifact a reader
downloads did not pass its own `python3 -m floati.selftest`. Twenty-nine
failures, every one of them a test whose subject the export had removed.

⇒ **AN EXCLUSION LIST THAT NAMES SUBJECTS BUT NOT THEIR TESTS SHIPS A SUITE
THAT TESTS ABSENCE**, and nothing noticed because the exporter validates the
projected tree and has never run it.

A module whose whole subject is private belongs in the policy's
`private_only_paths`. This helper is for the other case: a mostly-public module
with one or two tests that reach a private artifact. Those become a **stated
skip** rather than an error, because a reader is entitled to know the
difference between "this did not run here" and "this failed".
"""

from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def require_private_artifact(case, *relatives: str) -> None:
    """Skip `case` when any named repository-relative artifact is absent.

    Absent means *this tree does not carry it* — in the public projection that
    is by policy, and the skip reason says so rather than implying breakage.
    """

    missing = [
        relative
        for relative in relatives
        if not (REPOSITORY_ROOT / relative).exists()
    ]
    if missing:
        case.skipTest(
            "not in this tree (private to the harbor repository by export "
            "policy): " + ", ".join(missing)
        )

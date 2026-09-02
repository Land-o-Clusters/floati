"""Every committed capture is a photograph, so something must watch it for drift.

A capture that no test reads goes stale silently: the renderer changes, the committed
bytes do not, and the published screenshot contradicts the published source. That is not
hypothetical - it happened on 2026-08-30, when a demo fixture rename turned twelve guarded
captures RED and left the unguarded ones showing node names the code no longer produces.

This file does not check currency itself; each capture's own bank does that. It checks the
weaker, structural thing nobody was checking: THAT A BANK EXISTS. The set of exceptions is
declared, so the debt is visible and countable, and the assertion is an equality rather
than a subset - declaring an exception that has since been guarded fails too, which is what
stops this list from outliving its reasons.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CAPTURE_ROOT = REPOSITORY_ROOT / "docs/evidence/captures"

# Captures no test reads today, each with the reason it is not simply fixed.
UNGUARDED = {
    "floati-orchestrate-drill.txt": "no committed producer; provenance unknown",
    "floati-replay-live.txt": "no committed producer; provenance unknown",
    "hm1-tui-color.txt": "producer is the Makefile demo-capture target, not a test",
    "hm1-tui-monochrome.txt": "producer is the Makefile demo-capture target, not a test",
}


class CaptureCurrencyTests(unittest.TestCase):
    def test_every_committed_capture_is_watched_by_some_test(self) -> None:
        """Adding an unwatched capture, or leaving a guarded one declared unguarded, is rejected."""

        captures = sorted(path.name for path in CAPTURE_ROOT.glob("*.txt"))
        # Count anchor: without it, a glob regression would find nothing and pass green.
        # The floor is the SMALLER of the two trees this file runs in. The harbor holds
        # 20; the public projection holds 19, because one capture is excluded from the
        # export by policy (it holds the retired repository name and a photograph is not
        # rewritten). This test enumerates a directory and cannot read the export policy,
        # which is harbor-only, so the floor is stated rather than derived.
        self.assertGreaterEqual(
            len(captures), 19, f"capture enumeration collapsed: {captures}"
        )

        sources = [
            path.read_text(encoding="utf-8", errors="replace")
            for path in sorted((REPOSITORY_ROOT / "tests").glob("*.py"))
            if path.name != Path(__file__).name
        ]
        unwatched = [
            name for name in captures if not any(name in source for source in sources)
        ]

        self.assertEqual(
            sorted(UNGUARDED),
            unwatched,
            "the declared-unguarded set must match reality exactly: guard a capture and "
            "remove it from UNGUARDED, or declare a new one with its reason",
        )


if __name__ == "__main__":
    unittest.main()

"""K4 zcode bus-tier C-row — the live exercise receipt, watched by the bank.

The LIVE turn itself is never run by the bank (promo tokens are the
owner's spend; no test may reach a model). This bank watches the
COMMITTED receipt of one bounded live exercise: raw artifact, stderr,
and the launched command, sha256-pinned, under
docs/evidence/gauntlet/captures/zc1-k4-zcode-live/.

Contract asserted against the committed artifact (Am.2 measured shape):
sessionId · traceId · turnId · response · usage.source == "provider"
with positive inputTokens. Failure detection stays rc/stderr-shaped:
this test never decides success by parsing stdout — it pins what a
SUCCESSFUL turn's stdout looked like, after the run, from the capture.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


CAPTURE_DIR = (
    Path("docs/evidence/gauntlet/captures/zc1-k4-zcode-live"))
CAPTURE_FILES = (
    "attempt1-stdout-empty.txt",
    "attempt1-stderr-helpdump.txt",
    "attempt1-meta.txt",
    "live-stdout.json",
    "live-stderr.txt",
    "live-meta.txt",
)
CONTRACT_KEYS = ("sessionId", "traceId", "turnId", "response", "usage")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ZcodeLiveExerciseReceiptTests(unittest.TestCase):
    def test_capture_directory_is_committed(self) -> None:
        self.assertTrue(
            CAPTURE_DIR.is_dir(),
            f"missing {CAPTURE_DIR}: the K4 live exercise has not run")

    def test_every_capture_file_is_present_and_hash_pinned(self) -> None:
        pinned = {}
        for line in (CAPTURE_DIR / "sha256s.txt").read_text(
                encoding="utf-8").splitlines():
            digest, _, name = line.strip().partition("  ")
            if digest and name:
                pinned[name.strip()] = digest
        self.assertEqual(
            len(pinned), len(CAPTURE_FILES),
            "sha256s.txt pins exactly every capture file")
        for name in CAPTURE_FILES:
            with self.subTest(capture=name):
                path = CAPTURE_DIR / name
                self.assertTrue(path.is_file(), f"missing {name}")
                self.assertEqual(
                    sha256_file(path), pinned[name],
                    f"{name}: committed bytes do not match the pinned hash")

    def test_live_artifact_carries_the_measured_contract(self) -> None:
        artifact = json.loads(
            (CAPTURE_DIR / "live-stdout.json").read_text(encoding="utf-8"))
        for key in CONTRACT_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, artifact)
        usage = artifact["usage"]
        self.assertEqual("provider", usage.get("source"))
        self.assertGreater(int(usage.get("inputTokens", 0)), 0)
        self.assertGreater(int(usage.get("outputTokens", 0)), 0)

    def test_receipt_names_the_launched_executable(self) -> None:
        """C-row element 1: the live executable is NAMED — the pinned
        command tuple appears verbatim in the meta capture."""
        meta = (CAPTURE_DIR / "live-meta.txt").read_text(encoding="utf-8")
        self.assertIn("/opt/homebrew/bin/node", meta)
        self.assertIn(
            "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs", meta)
        self.assertIn("return_code=0", meta,
                      "the receipt records a live rc=0 turn")


if __name__ == "__main__":
    unittest.main()

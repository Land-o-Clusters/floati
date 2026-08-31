"""The zcode wake cell's matrix-citation contract (CI-1 composition).

Renamed off ``tests/test_export_public.py`` at landing: the CI-1 candidate's
863-line export-contract bank owns that path, and two different files there is a
silent-loss hazard rather than a merge inconvenience.  See
``docs/evidence/zc1-row-a-gate-2026-08-30.md``.

Ruled in `docs/evidence/ci1-landing-merge-gate-2026-08-30.md` (R6): the
capability matrix is a public artifact, and **a citation a reader cannot
follow is worse than no citation**. The zcode/cli/wake cell cited a private
gate verdict at a bare `docs/evidence/` prefix; this bank pins its replacement:
a PUBLIC-SAFE MX1 receipt, enumerated in the hand-written
`additional_public_paths` exception list.

Scope note: the candidate's own bank polices every matrix receipt; this
one pins the cell this lane measured, and the closure of the exception
entry it contributed.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]
MATRIX = REPOSITORY_ROOT / "docs" / "capability-matrix.v0.json"

PUBLIC_ZCODE_WAKE_RECEIPT = "docs/evidence/gauntlet/MX1-zcode-cli-wake.md"

# The hand-written exception list (R6): receipts cleared public-safe
# despite living outside the auto-include conformance/ prefix. Closure is
# checked by derivation — every entry must be cited by the matrix.
ADDITIONAL_PUBLIC_PATHS = (PUBLIC_ZCODE_WAKE_RECEIPT,)


class ZcodeWakeExportPublicTests(unittest.TestCase):
    def zcode_wake_record(self) -> dict:
        matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
        records = [
            record
            for record in matrix["records"]
            if record.get("harness") == "zcode"
            and record.get("surface") == "cli"
            and record.get("capability") == "wake"
        ]
        self.assertEqual(1, len(records), "the zcode wake cell is unique")
        return records[0]

    def test_zcode_wake_receipt_is_public_and_staged(self) -> None:
        """A public reader must be able to open the cell's receipt: the
        path is the public MX1 receipt, it is committed, and the private
        gate verdict is not cited."""
        record = self.zcode_wake_record()
        self.assertEqual(PUBLIC_ZCODE_WAKE_RECEIPT, record["receipt_path"])
        self.assertTrue(
            (REPOSITORY_ROOT / record["receipt_path"]).is_file(),
            f"{record['receipt_path']} is not staged in the published tree")
        self.assertNotIn(
            "wd-r2-gate", record["receipt_path"],
            "the private gate verdict must not be the public citation")

    def test_additional_public_paths_names_only_cited_receipts(self) -> None:
        """The exception list declares a closed set — it must contain the
        set. An entry nothing cites is drift; the count is derived here,
        never restated."""
        cited = {
            record.get("receipt_path")
            for record in json.loads(
                MATRIX.read_text(encoding="utf-8"))["records"]
        }
        stale = [path for path in ADDITIONAL_PUBLIC_PATHS if path not in cited]
        self.assertEqual([], stale)


if __name__ == "__main__":
    unittest.main()

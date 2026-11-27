from __future__ import annotations

import json
import re
import tempfile
import subprocess
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DATASET = REPOSITORY_ROOT / "docs" / "capability-matrix.v0.json"
RENDERER = REPOSITORY_ROOT / "scripts" / "capability-matrix-render.py"
CURRENT_RECEIPT = "docs/evidence/conformance/C2-claude-cli-version-2026-09-03.md"


class CapabilityMatrixVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dataset = json.loads(DATASET.read_text(encoding="utf-8"))

    def render(self, mode: str) -> str:
        return subprocess.run(
            ["/usr/bin/python3", str(RENDERER), "--mode", mode],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_output_file_links_resolve_to_the_same_receipts_as_stdout(self) -> None:
        for mode in ("compact", "full"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "nested" / "matrix.md"
                output.parent.mkdir()
                subprocess.run(
                    ["/usr/bin/python3", str(RENDERER), "--mode", mode,
                     "--output", str(output)],
                    cwd=REPOSITORY_ROOT, check=True, capture_output=True, text=True,
                )
                original = re.findall(r"\]\(([^)]+)\)", self.render(mode))
                written = re.findall(r"\]\(([^)]+)\)", output.read_text())
                self.assertEqual(len(original), len(written))
                self.assertGreater(len(original), 0)
                for source, rendered in zip(original, written):
                    self.assertEqual(
                        (REPOSITORY_ROOT / source).resolve(),
                        (output.parent / rendered).resolve(),
                        "output links must remain bound to the same repository receipt",
                    )

    def test_declared_current_claude_version_is_cited_by_its_measured_receipt(self) -> None:
        current = self.dataset["declared_current_versions"]["claude/cli"]

        self.assertEqual("2.1.251 (Claude Code)", current["version"])
        self.assertEqual("2026-09-03", current["measured_at"])
        self.assertEqual(CURRENT_RECEIPT, current["receipt_path"])
        receipt = (REPOSITORY_ROOT / current["receipt_path"]).read_text(encoding="utf-8")
        self.assertIn(current["version"], receipt)

    def test_claude_cell_version_stamps_stay_bound_to_their_receipts(self) -> None:
        expected = {
            ("auto_turnover", "docs/evidence/gauntlet/T1-depth2.md", "2.1.231 (Claude Code)"),
            ("boot", "docs/evidence/WS-D3-NODE-LIFECYCLE-PROJECTION-WIRING.md", "2.1.231 (Claude Code)"),
            ("bus", "docs/evidence/conformance/C2-claude-conformance-live-2026-09-04.md", "2.1.251 (Claude Code)"),
            ("compaction", "docs/evidence/gauntlet/T1-tide-survey.md", "2.1.231 (Claude Code)"),
            ("managed_send", "docs/evidence/conformance/C0-managed-send-surface.md", "2.1.231 (Claude Code)"),
            ("wake", "docs/evidence/conformance/H-claude-wake-remeasure-2026-09-04.md", "2.1.251 (Claude Code)"),
            ("work", "docs/evidence/conformance/C2-claude-conformance-live-2026-09-04.md", "2.1.251 (Claude Code)"),
        }
        actual = {
            (record["capability"], record["receipt_path"], record["versions"]["cli"])
            for record in self.dataset["records"]
            if record["harness"] == "claude" and record["surface"] == "cli"
        }

        self.assertEqual(expected, actual)
        self.assertTrue(
            all("version_stale" not in record for record in self.dataset["records"]),
            "version_stale is a derived render fact, never stored in a cell",
        )

    def test_renderer_marks_every_stale_claude_cell_without_restamping_it(self) -> None:
        full = self.render("full")
        compact = self.render("compact")
        full_row = next(line for line in full.splitlines() if line.startswith("| claude / cli |"))
        compact_row = next(line for line in compact.splitlines() if line.startswith("| claude |"))

        self.assertEqual(4, full_row.count("`version_stale: true`"))
        self.assertEqual(0, compact_row.count("`version_stale: true`"))
        for rendered in (full, compact):
            self.assertEqual(1, rendered.count("Version honesty:"))
            self.assertIn("2.1.231 (Claude Code)", rendered)
            self.assertNotIn("2.1.238 (Claude Code)", rendered)
            self.assertIn("2.1.251 (Claude Code)", rendered)
            self.assertIn(CURRENT_RECEIPT, rendered)

    def test_t3_rows_link_current_evidence_and_mark_august_cells_stale(self) -> None:
        for mode in ("full", "compact"):
            rendered = self.render(mode)
            cli_row = "| t3 / cli |" if mode == "full" else "| t3 |"
            for surface, row_start, historical_version in (
                ("cli", cli_row, "t3 v0.0.35"),
                ("desktop", "| t3 / desktop |", "T3 Code Nightly 0.0.35-nightly.20260826.1195"),
            ):
                with self.subTest(mode=mode, surface=surface):
                    coordinate = f"t3/{surface}"
                    current = self.dataset["declared_current_versions"].get(coordinate)
                    self.assertIsNotNone(current, f"{coordinate} has no current-build receipt")
                    self.assertNotEqual(historical_version, current["version"])
                    receipt = REPOSITORY_ROOT / current["receipt_path"]
                    self.assertIn(current["version"], receipt.read_text(encoding="utf-8"))
                    self.assertIn(f"]({current['receipt_path']})", rendered)

                    row = next(line for line in rendered.splitlines() if line.startswith(row_start))
                    cells = row.split("|")[2:-1]
                    if mode == "compact":
                        cells = cells[:-1]  # The final column is a row note.
                    self.assertTrue(cells)
                    for cell in cells:
                        self.assertIn("`version_stale: true`", cell)


if __name__ == "__main__":
    unittest.main()

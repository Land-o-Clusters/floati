"""LEDGER-1 (c): doctor reports UNMANAGED bytes inside the fleet root.

The root layout is the product's declared families (ledger, registry,
nodes, state, receipts, cursors, authority-grants, work, codex-wait,
scratch, effects, liveness-presence, runs, sequencer, archive-*,
quarantine-*). Anything else at the top level is reported with its path
and recursive size — and never touched: floati deleting an unmanaged
entry is the LANES-1 failure mode. Measured on the live root: hand-made
`tmp/` blobs sit inside the bus root invisible to every health surface.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from floati.doctor import project_root_layout_findings
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


class RootLayoutFindingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root_path = Path(self.temporary.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.root_path, create=True)

    def test_planted_blob_is_reported_with_path_and_size(self) -> None:
        """RED: a planted blob under <root>\x2ftmp/ is invisible today."""

        blob = self.root_path / "tmp" / "blob.bin"
        blob.parent.mkdir()
        blob.write_bytes(b"x" * 5000)
        nested = self.root_path / "tmp" / "deep" / "more.bin"
        nested.parent.mkdir()
        nested.write_bytes(b"y" * 1234)

        findings = project_root_layout_findings(self.root)

        rows = [row for row in findings if row["code"] == "root_unmanaged_bytes"]
        self.assertEqual(1, len(rows), findings)
        self.assertEqual("warning", rows[0]["severity"])
        self.assertEqual("tmp", rows[0]["subject"])
        self.assertIn("6234", rows[0]["detail"])
        self.assertIn("never", rows[0]["detail"])
        self.assertTrue(blob.is_file(), "the unmanaged blob must be untouched")
        self.assertTrue(nested.is_file(), "unmanaged content must be untouched")

    def test_managed_families_are_never_reported(self) -> None:
        for family in (
            "registry", "nodes", "state", "receipts", "cursors",
            "authority-grants", "work", "codex-wait", "scratch", "effects",
            "liveness-presence", "runs", "sequencer",
        ):
            (self.root_path / family / "marker").mkdir(parents=True)
        (self.root_path / "events.jsonl").write_text("{}\n", encoding="utf-8")
        (self.root_path / "events.jsonl.lock").write_text("", encoding="utf-8")
        (self.root_path / "archive-2026-08-29").mkdir()
        (self.root_path / "quarantine-2026-08-29.jsonl").write_text(
            "{}\n", encoding="utf-8"
        )

        findings = project_root_layout_findings(self.root)

        self.assertEqual(
            [], [row for row in findings if row["code"] == "root_unmanaged_bytes"],
            findings,
        )
        self.assertEqual("root_layout_managed", findings[0]["code"])

    def test_unmanaged_symlink_is_reported_without_walking_its_target(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "big.bin").write_bytes(b"z" * 10_000)
        link = self.root_path / "stash"
        link.symlink_to(outside, target_is_directory=True)

        findings = project_root_layout_findings(self.root)

        rows = [row for row in findings if row["code"] == "root_unmanaged_bytes"]
        self.assertEqual(1, len(rows), findings)
        self.assertEqual("stash", rows[0]["subject"])
        self.assertNotIn("10000", rows[0]["detail"], "a symlink's target is not walked")


class RootLayoutDoctorWiringTests(unittest.TestCase):
    def test_doctor_artifact_carries_the_unmanaged_row(self) -> None:
        """The check rides the real doctor artifact, not just the projector."""

        from floati.doctor import Doctor

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            home = Path(temporary) / "fleet"
            root = FloatiRoot.open_direct_home(home, create=True)
            (home / "tmp").mkdir()
            (home / "tmp" / "blob.bin").write_bytes(b"x" * 5000)

            doctor = Doctor(
                Path(__file__).resolve().parents[1],
                home,
                ref="origin/main",
                no_sandbox=True,
            )
            artifact, _rc = doctor.artifact()

            findings = artifact.get("findings", [])
            codes = [row["code"] for row in findings]
            self.assertIn("root_unmanaged_bytes", codes, codes[:5])


if __name__ == "__main__":
    unittest.main()


class RootLayoutDerivedFamiliesTests(unittest.TestCase):
    """LEDGER-1 (c) Am.1: the families are DERIVED from the constants
    that own them — the hand list missed .floati-snapshots, a directory
    the product writes into the live root today (1.99 MB), so doctor
    flagged a product directory on day one."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root_path = Path(self.temporary.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.root_path, create=True)

    def test_live_shaped_snapshot_and_intake_directories_are_managed(self) -> None:
        """RED: .floati-snapshots was reported unmanaged."""

        (self.root_path / ".floati-snapshots" / "v0").mkdir(parents=True)
        (self.root_path / ".floati-snapshots" / "v0" / "snap.json").write_text(
            "{}\n", encoding="utf-8"
        )
        (self.root_path / "intake" / "v0").mkdir(parents=True)
        (self.root_path / "intake" / "v0" / "payload.json").write_text(
            "{}\n", encoding="utf-8"
        )

        findings = project_root_layout_findings(self.root)

        self.assertEqual(
            [],
            [row for row in findings if row["code"] == "root_unmanaged_bytes"],
            findings,
        )
        self.assertEqual("root_layout_managed", findings[0]["code"])

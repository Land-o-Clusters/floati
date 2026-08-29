from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


REQUIRED_PHASE1_PATHS = (
    ".github/workflows/selftest.yml",
    "LICENSE",
    "schemas/LICENSE",
    "bundle/c7.1/LICENSE",
    "bundle/c7.2/LICENSE",
    "bundle-manifest.v0.json",
    "docs/FLEET.md",
    "docs/TRUTH-GUARANTEES.md",
    "schemas/v0/ack-receipt.schema.json",
    "schemas/v0/authority-grant-record.schema.json",
    "schemas/v0/delivery-receipt.schema.json",
    "schemas/v0/liveness-presence-record.schema.json",
    "schemas/v0/message-envelope.schema.json",
    "schemas/v0/mutual-exclusion-hold-record.schema.json",
    "schemas/v0/registry-entry.schema.json",
    "schemas/v0/wake-cause-record.schema.json",
    "floati/conformance.py",
    "floati/cli.py",
    "floati/cursor.py",
    "floati/events.py",
    "floati/jsonl.py",
    "floati/manifest.py",
    "floati/planes.py",
    "floati/registry.py",
    "floati/root.py",
    "floati/scrub.py",
    "floati/__main__.py",
    "scripts/floati",
)


class Phase1ContractTests(unittest.TestCase):
    def test_required_phase1_paths_exist(self) -> None:
        missing = [path for path in REQUIRED_PHASE1_PATHS if not Path(path).is_file()]
        self.assertEqual([], missing, f"missing phase-1 artifacts: {missing}")

    def test_ratified_license_files_replace_the_placeholder(self) -> None:
        self.assertFalse(Path("LICENSE-PENDING.md").exists())
        self.assertTrue(Path("LICENSE").is_file())
        self.assertTrue(Path("schemas/LICENSE").is_file())
        self.assertTrue(Path("bundle/c7.1/LICENSE").is_file())
        self.assertTrue(Path("bundle/c7.2/LICENSE").is_file())

    def test_fleet_contract_names_only_the_ruled_nodes_and_polling_boundary(self) -> None:
        fleet_path = Path("docs/FLEET.md")
        self.assertTrue(fleet_path.is_file(), "docs/FLEET.md must define fleet onboarding")
        fleet = fleet_path.read_text(encoding="utf-8")
        rows = set()
        for line in fleet.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) == 3 and cells[0] not in {"Node", "---"}:
                rows.add(cells[0])
        self.assertEqual({"reviewer", "lane-app", "lane-floati"}, rows)
        self.assertIn("Claude", fleet)
        self.assertIn("Codex", fleet)
        self.assertIn("tenant-a", fleet)
        self.assertIn("~/fleet", fleet)
        self.assertIn("at boot", fleet)
        self.assertIn("before stand-down", fleet)
        self.assertIn("Each node registers itself", fleet)
        self.assertIn("Acknowledgment is not completion", fleet)

    def test_cli_refuses_an_omitted_root_and_has_no_wake_command(self) -> None:
        environment = dict(os.environ)
        environment.pop("FLOATI_BUS_ROOT", None)
        environment.pop("FLOATI_BUS_CONFIG", None)
        for arguments in (("init",), ("inbox", "--as", "lane-floati"), ("wake",)):
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    [sys.executable, "-m", "floati", *arguments],
                    cwd=Path.cwd(),
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                self.assertEqual("", completed.stdout)
                artifact = json.loads(completed.stderr)
                if arguments[0] in {"init", "inbox"}:
                    self.assertEqual(22, completed.returncode)
                    self.assertEqual("cannot_speak", artifact["status"])
                    self.assertEqual("cannot_speak", artifact["evidence"]["code"])
                else:
                    self.assertEqual(20, completed.returncode)
                    self.assertEqual("refused", artifact["status"])
                    self.assertEqual("arguments_invalid", artifact["evidence"]["code"])

    def test_tracked_launcher_is_executable(self) -> None:
        self.assertTrue(Path("scripts/floati").stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()

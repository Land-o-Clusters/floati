from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_cli import LAUNCHER

from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.root import FloatiRoot


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class Roots1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.alpha = FloatiRoot.open_direct_home(self.base / "alpha", create=True)
        self.beta = FloatiRoot.open_direct_home(self.base / "beta", create=True)
        self.gamma = FloatiRoot.open_direct_home(self.base / "gamma", create=True)
        self._registry(self.alpha, "architect-a")
        self._registry(self.beta, "architect-b")
        self._registry(self.gamma, "architect-c")
        self.declared = self.base / "declared-roots.json"
        self.declared.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "roots": [
                        {
                            "bus_id": "alpha",
                            "root": str(self.alpha.path),
                            "architect_node": "architect-a",
                            "downstream": ["beta"],
                        },
                        {
                            "bus_id": "beta",
                            "root": str(self.beta.path),
                            "architect_node": "architect-b",
                            "downstream": [],
                        },
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _registry(root: FloatiRoot, node: str) -> None:
        append_record(
            root,
            "registry/entries.jsonl",
            {
                "schema_version": 0,
                "id": "registry-" + uuid7_hex(),
                "tenant_id": root.tenant_id,
                "timestamp": "2026-08-27T21:58:00.000Z",
                "kind": "registry_entry",
                "node_id": node,
                "role": "Architect",
                "state": "active",
            },
            allowed_kinds={"registry_entry"},
        )

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(LAUNCHER), *arguments],
            cwd=REPOSITORY_ROOT,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            text=True,
            capture_output=True,
            check=False,
        )

    def test_chart_add_root_and_remove_root_are_receipted_rewrites(self) -> None:
        """Catches add-root remaining an unrecognized chart subcommand."""

        added = self.run_cli(
            "chart",
            "add-root",
            "--declared-roots",
            str(self.declared),
            "--bus-id",
            "gamma",
            "--root",
            str(self.gamma.path),
            "--architect-node",
            "architect-c",
        )
        self.assertEqual(0, added.returncode, added.stderr)
        artifact = json.loads(added.stdout)
        self.assertEqual("ok", artifact["status"])
        self.assertEqual("add-root", artifact["evidence"]["operation"])
        self.assertEqual(["alpha", "beta", "gamma"], artifact["evidence"]["roots"])

        removed = self.run_cli(
            "chart",
            "remove-root",
            "--declared-roots",
            str(self.declared),
            "--bus-id",
            "gamma",
        )
        self.assertEqual(0, removed.returncode, removed.stderr)
        self.assertEqual(["alpha", "beta"], json.loads(removed.stdout)["evidence"]["roots"])

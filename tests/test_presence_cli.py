from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati.planes import LivenessPresenceStore
from floati.registry import Registry
from floati.root import FloatiRoot


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-01T17:00:00Z"


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


class PresenceCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        registry = Registry(self.root)
        registry.register("alpha", "Codex")
        registry.register("bravo", "Codex")

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "floati", *arguments],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def artifact(result: subprocess.CompletedProcess[str]) -> dict:
        return json.loads(result.stdout)

    def test_report_has_only_the_acting_node_and_records_its_explicit_ttl(self) -> None:
        before_report = datetime.now(timezone.utc)
        reported = self.run_cli(
            "presence",
            "report",
            "--root",
            str(self.home),
            "--as",
            "alpha",
            "--ttl-seconds",
            "120",
        )
        after_report = datetime.now(timezone.utc)

        self.assertEqual(0, reported.returncode, reported.stderr)
        evidence = self.artifact(reported)["evidence"]
        self.assertEqual("alpha", evidence["node_id"])
        observed_at = datetime.fromisoformat(evidence["observed_at"].replace("Z", "+00:00"))
        expires_at = datetime.fromisoformat(evidence["expires_at"].replace("Z", "+00:00"))
        self.assertLessEqual(before_report, observed_at)
        self.assertLessEqual(observed_at, after_report)
        self.assertEqual(120, int((expires_at - observed_at).total_seconds()))
        self.assertTrue((self.root.tenant_home / "liveness-presence/alpha.jsonl").is_file())
        self.assertFalse((self.root.tenant_home / "liveness-presence/bravo.jsonl").exists())

        before = tree_digest(self.home)
        attempted_time_forgery = self.run_cli(
            "presence",
            "report",
            "--root",
            str(self.home),
            "--as",
            "alpha",
            "--ttl-seconds",
            "120",
            "--now",
            NOW,
        )
        self.assertEqual(20, attempted_time_forgery.returncode)
        self.assertEqual(
            "arguments_invalid",
            self.artifact(attempted_time_forgery)["evidence"]["code"],
        )
        self.assertEqual(before, tree_digest(self.home))

        attempted_proxy = self.run_cli(
            "presence",
            "report",
            "--root",
            str(self.home),
            "--as",
            "alpha",
            "--node",
            "bravo",
            "--ttl-seconds",
            "120",
        )
        self.assertEqual(20, attempted_proxy.returncode)
        self.assertEqual("arguments_invalid", self.artifact(attempted_proxy)["evidence"]["code"])
        self.assertEqual(before, tree_digest(self.home))

    def test_show_names_each_nodes_report_time_ttl_and_honest_expiry(self) -> None:
        LivenessPresenceStore(self.root).observe(
            "alpha",
            ttl_seconds=120,
            now=datetime.fromisoformat(NOW.replace("Z", "+00:00")),
        )

        before = tree_digest(self.home)
        shown = self.run_cli(
            "presence",
            "show",
            "--root",
            str(self.home),
        )
        self.assertEqual(0, shown.returncode, shown.stderr)
        self.assertEqual(before, tree_digest(self.home))
        reports = self.artifact(shown)["evidence"]["reports"]
        self.assertEqual(
            [
                {
                    "expires_at": "2026-09-01T17:02:00.000Z",
                    "node_id": "alpha",
                    "reported_at": "2026-09-01T17:00:00.000Z",
                    "state": "no_report_since",
                    "ttl_seconds": 120,
                },
                {
                    "expires_at": None,
                    "node_id": "bravo",
                    "reported_at": None,
                    "state": "never_reported",
                    "ttl_seconds": None,
                },
            ],
            reports,
        )
        self.assertNotIn("down", json.dumps(reports).casefold())

    def test_retired_or_unknown_node_cannot_create_presence_evidence(self) -> None:
        Registry(self.root).retire("bravo")
        before = tree_digest(self.home)
        refused = self.run_cli(
            "presence",
            "report",
            "--root",
            str(self.home),
            "--as",
            "bravo",
            "--ttl-seconds",
            "120",
        )
        self.assertEqual(20, refused.returncode)
        self.assertEqual("unknown_node", self.artifact(refused)["evidence"]["code"])
        self.assertEqual(before, tree_digest(self.home))


if __name__ == "__main__":
    unittest.main()

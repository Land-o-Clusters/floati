from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_cli import LAUNCHER, SHA

from floati import fixture_ids as public_ids
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DRAIN_SESSION = "drain-session"


class Drain1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "fleet"
        initialized = self.run_cli("init", "--root", str(self.root))
        self.assertEqual(0, initialized.returncode, initialized.stderr)
        sender = self.run_cli(
            "register", "--root", str(self.root), "architect-a", "--harness", "Codex"
        )
        self.assertEqual(0, sender.returncode, sender.stderr)
        holder = self.run_cli(
            "register",
            "--root",
            str(self.root),
            public_ids.builder("a"),
            "--harness",
            "Codex",
        )
        self.assertEqual(0, holder.returncode, holder.stderr)

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(LAUNCHER), *arguments],
            cwd=REPOSITORY_ROOT,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            text=True,
            capture_output=True,
            check=False,
        )

    def artifact(self, result: subprocess.CompletedProcess[str]) -> dict:
        self.assertEqual("", result.stderr, result.stderr)
        return json.loads(result.stdout)

    def send_mail(self) -> str:
        sent = self.run_cli(
            "send",
            "--root",
            str(self.root),
            "--from",
            "architect-a",
            "--to",
            public_ids.builder("a"),
            "--repo",
            "floati",
            "--sha",
            SHA,
            "--doc",
            "docs/evidence/scratch.md",
            "--note",
            "drain-1 fixture",
        )
        self.assertEqual(0, sent.returncode, sent.stderr)
        return str(self.artifact(sent)["evidence"]["message"]["id"])

    def peek(self) -> list[str]:
        peeked = self.run_cli(
            "inbox",
            "--root",
            str(self.root),
            "--as",
            public_ids.builder("a"),
            "--peek",
        )
        artifact = json.loads(peeked.stdout)
        if peeked.returncode == 31:
            self.assertEqual("intentional_silence", artifact["status"])
            return []
        self.assertEqual(0, peeked.returncode, peeked.stderr)
        return [
            str(message["id"])
            for message in artifact["evidence"]["messages"]
        ]

    def test_node_drain_empties_inbox_and_leaves_the_node_registered(self) -> None:
        """Catches drain composing with retire, or leaving unacked mail behind."""

        message_id = self.send_mail()
        self.assertEqual([message_id], self.peek())

        drained = self.run_cli(
            "node",
            "drain",
            "--root",
            str(self.root),
            "--node",
            public_ids.builder("a"),
            "--session",
            DRAIN_SESSION,
        )
        self.assertEqual(0, drained.returncode, drained.stderr)
        evidence = self.artifact(drained)["evidence"]
        self.assertEqual("ok", self.artifact(drained)["status"])
        self.assertEqual(public_ids.builder("a"), evidence["node_id"])
        self.assertEqual([message_id], evidence["acked_ids"])
        self.assertEqual(DRAIN_SESSION, evidence["acting_session_id"])
        self.assertTrue(evidence["registered"])
        self.assertEqual("active", evidence["state"])
        self.assertEqual([], self.peek())

        fleet = FloatiRoot.open_direct_home(self.root)
        self.assertIn(public_ids.builder("a"), Registry(fleet).active_node_ids())
        self.assertEqual([], EventLog(fleet).unacked_ids(public_ids.builder("a")))

        again = self.run_cli(
            "node",
            "drain",
            "--root",
            str(self.root),
            "--node",
            public_ids.builder("a"),
            "--session",
            DRAIN_SESSION,
        )
        self.assertEqual(0, again.returncode, again.stderr)
        self.assertEqual([], self.artifact(again)["evidence"]["acked_ids"])
        self.assertIn(public_ids.builder("a"), Registry(fleet).active_node_ids())

    def test_node_drain_refuses_an_unknown_node(self) -> None:
        missing = self.run_cli(
            "node",
            "drain",
            "--root",
            str(self.root),
            "--node",
            "missing-node",
            "--session",
            DRAIN_SESSION,
        )
        self.assertEqual(20, missing.returncode, missing.stderr)
        artifact = json.loads(missing.stdout)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("unknown_node", artifact["evidence"]["code"])

    def test_node_drain_without_session_refuses_naming_session(self) -> None:
        """Catches a product-minted drain session standing in for a declared one."""

        missing = self.run_cli(
            "node",
            "drain",
            "--root",
            str(self.root),
            "--node",
            public_ids.builder("a"),
        )
        self.assertEqual(20, missing.returncode, missing.stderr)
        artifact = json.loads(missing.stdout)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("arguments_invalid", artifact["evidence"]["code"])
        self.assertIn("--session", artifact["evidence"]["detail"])

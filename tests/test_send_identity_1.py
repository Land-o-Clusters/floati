"""SEND-IDENTITY-1: send and ack honor the declared-identity gate.

Inbox already refuses a present-marker mismatch before any ledger is opened.
Send and ack did not. This module constructs that write on both verbs, then
requires the same ``workspace_identity_mismatch`` fence inbox uses — absent
markers stay silent, a disagreeing declaration does not mutate the root.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
COORDINATOR_AUTHORITY = (
    "dispatch_bounded_work",
    "gate_results_before_merge",
    "decide_non_owner_tier_questions",
)
OWNER_TIER = ("publishing", "credentials", "key_custody")


def tree_snapshot(root: Path) -> dict[str, tuple[str, bytes]]:
    return {
        path.relative_to(root).as_posix(): (
            "symlink"
            if path.is_symlink()
            else "directory"
            if path.is_dir()
            else "file",
            b"" if path.is_symlink() or path.is_dir() else path.read_bytes(),
        )
        for path in root.rglob("*")
    }


class SendIdentity1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def invoke(
        self, *arguments: str, cwd: Path = REPOSITORY_ROOT
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        environment = dict(os.environ)
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            value
            for value in (str(REPOSITORY_ROOT), existing_pythonpath)
            if value
        )
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-m", "floati", *arguments],
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual("", completed.stderr)
        self.assertTrue(completed.stdout.endswith("\n"))
        self.assertEqual(1, len(completed.stdout.splitlines()))
        artifact = json.loads(completed.stdout)
        return completed, artifact

    def initialize_root(self, root: Path, *nodes: str) -> None:
        initialized, _artifact = self.invoke("init", "--root", str(root))
        self.assertEqual(0, initialized.returncode, initialized.stderr)
        for node in nodes:
            added, _artifact = self.invoke(
                "node", "add", "--root", str(root), "--node", node,
                "--harness", "Codex", "--lifetime", "permanent",
            )
            self.assertEqual(0, added.returncode, added.stderr)

    def write_seat_fixture(self, workspace: Path, root: Path, node: str) -> None:
        (workspace / "SEAT.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "tenant_id": root.name,
                    "root": str(root.resolve()),
                    "node_id": node,
                    "topology": "star",
                    "coordinator": "architect-a",
                    "coordinator_authority": list(COORDINATOR_AUTHORITY),
                    "owner_tier": list(OWNER_TIER),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

    def mismatched_seat(self) -> tuple[Path, Path, Path]:
        declared_root = self.base / "declared-fleet"
        foreign_root = self.base / "foreign-fleet"
        node = public_ids.builder("a")
        self.initialize_root(declared_root, node)
        self.initialize_root(foreign_root, "sender-a", node)
        workspace = declared_root / "nodes" / node
        self.write_seat_fixture(workspace, declared_root, node)
        return declared_root, foreign_root, workspace

    def test_declared_root_mismatch_refuses_send_without_side_effect(self) -> None:
        _declared, foreign_root, workspace = self.mismatched_seat()
        before = tree_snapshot(foreign_root)

        completed, artifact = self.invoke(
            "send",
            "--root", str(foreign_root),
            "--from", public_ids.builder("a"),
            "--to", "sender-a",
            "--repo", "floati",
            "--sha", SHA,
            "--doc", "docs/evidence/task-2-red.md",
            "--note", "SEND-IDENTITY-1 constructs the write",
            cwd=workspace,
        )
        after = tree_snapshot(foreign_root)

        self.assertEqual(20, completed.returncode)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("workspace_identity_mismatch", artifact["evidence"]["code"])
        self.assertEqual(before, after)

    def test_declared_root_mismatch_refuses_ack_without_side_effect(self) -> None:
        _declared, foreign_root, workspace = self.mismatched_seat()
        sent, send_artifact = self.invoke(
            "send",
            "--root", str(foreign_root),
            "--from", "sender-a",
            "--to", public_ids.builder("a"),
            "--repo", "floati",
            "--sha", SHA,
            "--doc", "docs/evidence/task-2-red.md",
            "--note", "mail to ack from a mismatched seat",
        )
        self.assertEqual(0, sent.returncode, sent.stderr)
        message_id = send_artifact["evidence"]["message"]["id"]
        peeked, _peek = self.invoke(
            "inbox",
            "--root", str(foreign_root),
            "--as", public_ids.builder("a"),
            "--peek",
        )
        self.assertEqual(0, peeked.returncode, peeked.stderr)
        before = tree_snapshot(foreign_root)

        completed, artifact = self.invoke(
            "ack",
            "--root", str(foreign_root),
            "--as", public_ids.builder("a"),
            "--session", "send-identity-session",
            "--id", str(message_id),
            cwd=workspace,
        )
        after = tree_snapshot(foreign_root)

        self.assertEqual(20, completed.returncode)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("workspace_identity_mismatch", artifact["evidence"]["code"])
        self.assertEqual(before, after)

"""FQ-10 / BL-6: empty --note refuses at the CLI authoring boundary only.

``_bounded_note`` must keep reading legacy empty notes. The RED pair is:
(a) ``floati send --note ''`` refuses with ``note_empty``; (b) a durable
ledger fixture that already carries ``note: ""`` still reads. The fixture
file exists on disk before the test runs — it is not constructed by the
test.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.jsonl import read_records
from floati.root import FloatiRoot

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LEGACY_EMPTY_NOTE = (
    REPOSITORY_ROOT
    / "tests"
    / "fixtures"
    / "fq-10-legacy-empty-note"
    / "events.jsonl"
)
SHA = "a" * 40
RECORDS_PATH = REPOSITORY_ROOT / "floati" / "records.py"


class Fq10AuthoringNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def invoke(self, *arguments: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        environment = dict(os.environ)
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (str(REPOSITORY_ROOT), existing) if value
        )
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-m", "floati", *arguments],
            cwd=str(REPOSITORY_ROOT),
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual("", completed.stderr, completed.stderr)
        artifact = json.loads(completed.stdout)
        return completed, artifact

    def test_empty_note_refuses_at_send_authoring_boundary(self) -> None:
        root = self.base / "fleet"
        init, _ = self.invoke("init", "--root", str(root))
        self.assertEqual(0, init.returncode, init.stderr)
        sender = public_ids.builder("a")
        recipient = public_ids.builder("b")
        for node in (sender, recipient):
            added, _ = self.invoke(
                "node", "add", "--root", str(root), "--node", node,
                "--harness", "Codex", "--lifetime", "permanent",
            )
            self.assertEqual(0, added.returncode, added.stderr)
        before_events = (
            (root / "events.jsonl").read_bytes()
            if (root / "events.jsonl").is_file()
            else b""
        )

        completed, artifact = self.invoke(
            "send",
            "--root", str(root),
            "--from", sender,
            "--to", recipient,
            "--repo", "floati",
            "--sha", SHA,
            "--doc", "docs/evidence/checkpoint.md",
            "--note", "",
        )

        self.assertEqual(20, completed.returncode)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("note_empty", artifact["evidence"]["code"])
        self.assertIn("empty", artifact["evidence"]["detail"])
        self.assertTrue(str(artifact["evidence"].get("remedy") or "").strip())
        after_events = (
            (root / "events.jsonl").read_bytes()
            if (root / "events.jsonl").is_file()
            else b""
        )
        self.assertEqual(before_events, after_events)

    def test_whitespace_only_note_refuses_at_send_authoring_boundary(self) -> None:
        root = self.base / "fleet-ws"
        init, _ = self.invoke("init", "--root", str(root))
        self.assertEqual(0, init.returncode)
        sender = public_ids.builder("a")
        recipient = public_ids.builder("b")
        for node in (sender, recipient):
            self.invoke(
                "node", "add", "--root", str(root), "--node", node,
                "--harness", "Codex", "--lifetime", "permanent",
            )
        completed, artifact = self.invoke(
            "send",
            "--root", str(root),
            "--from", sender,
            "--to", recipient,
            "--repo", "floati",
            "--sha", SHA,
            "--doc", "docs/evidence/checkpoint.md",
            "--note", "   ",
        )
        self.assertEqual(20, completed.returncode)
        self.assertEqual("note_empty", artifact["evidence"]["code"])

    def test_legacy_empty_note_fixture_on_disk_still_reads(self) -> None:
        self.assertTrue(
            LEGACY_EMPTY_NOTE.is_file(),
            "FQ-10 control fixture must exist on disk before the test runs",
        )
        raw = LEGACY_EMPTY_NOTE.read_text(encoding="utf-8")
        self.assertIn('"note":""', raw.replace(" ", ""))

        root = FloatiRoot.open(self.base / "legacy", "alpha")
        destination = root.resolve_relative("events.jsonl")
        shutil.copyfile(LEGACY_EMPTY_NOTE, destination)

        durable = read_records(
            root, "events.jsonl", allowed_kinds={"message_envelope"}
        )
        self.assertEqual(1, len(durable))
        self.assertEqual("", durable[0]["note"])

    def test_bounded_note_was_not_tightened(self) -> None:
        source = RECORDS_PATH.read_text(encoding="utf-8")
        start = source.index("def _bounded_note")
        end = source.index("\ndef _bounded_string", start)
        body = source[start:end]
        self.assertNotIn("not value.strip()", body)
        self.assertNotIn("note is empty", body)


if __name__ == "__main__":
    unittest.main()

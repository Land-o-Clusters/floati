from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.root import FloatiRoot

try:
    from floati.journal_chain import JournalChain
except ModuleNotFoundError:
    JournalChain = None


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class JournalChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open(Path(self.temporary.name).resolve(), "alpha")
        self.relative = Path("journals/releases.jsonl")
        self.checkpoint_relative = Path("checkpoints/releases.json")
        self.journal_id = "release-journal"

    def _journal(self):
        self.assertIsNotNone(
            JournalChain,
            "floati.journal_chain must own S1 chaining and checkpoints",
        )
        return JournalChain(
            self.root,
            self.relative,
            journal_id=self.journal_id,
            allowed_kinds={"registry_entry"},
        )

    def _record(self, node: str, *, role: str = "worker") -> dict[str, object]:
        return {
            "schema_version": 0,
            "id": "registry-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-08-29T12:00:00.000Z",
            "kind": "registry_entry",
            "node_id": node,
            "role": role,
            "state": "active",
        }

    @property
    def _path(self) -> Path:
        return self.root.resolve_relative(self.relative)

    def _checkpoint(self, journal=None) -> dict[str, object]:
        selected = self._journal() if journal is None else journal
        return selected.write_checkpoint(self.checkpoint_relative)

    def test_s1_first_chained_line_anchors_last_legacy_exact_bytes(self) -> None:
        legacy = self._record("legacy")
        append_record(
            self.root,
            self.relative,
            legacy,
            allowed_kinds={"registry_entry"},
        )
        legacy_line = self._path.read_bytes().splitlines()[0]

        chained = self._journal().append(self._record("first"))

        self.assertEqual(1, chained["seq"])
        self.assertEqual(hashlib.sha256(legacy_line).hexdigest(), chained["prev"])
        self.assertNotIn("seq", legacy)
        self.assertNotIn("prev", legacy)

    def test_s1_exact_line_hash_excludes_only_lf(self) -> None:
        append_record(
            self.root,
            self.relative,
            self._record("legacy"),
            allowed_kinds={"registry_entry"},
        )
        legacy_line = self._path.read_bytes()[:-1]
        self._path.write_bytes(legacy_line + b"\r\n")

        chained = self._journal().append(self._record("first"))

        self.assertEqual(
            hashlib.sha256(legacy_line + b"\r").hexdigest(),
            chained["prev"],
        )

    def test_s1_empty_journal_genesis_hashes_journal_id(self) -> None:
        chained = self._journal().append(self._record("first"))

        self.assertEqual(1, chained["seq"])
        self.assertEqual(
            hashlib.sha256(self.journal_id.encode("utf-8")).hexdigest(),
            chained["prev"],
        )

    def test_s1_checkpoint_is_standalone_exact_json_for_the_bounded_prefix(self) -> None:
        journal = self._journal()
        journal.append(self._record("first"))
        journal.append(self._record("second"))

        checkpoint = self._checkpoint(journal)
        raw = self.root.resolve_relative(self.checkpoint_relative).read_bytes()
        lines = self._path.read_bytes().splitlines()

        self.assertEqual(
            {
                "format": "floati-journal-checkpoint-v1",
                "journal_id": self.journal_id,
                "through_seq": 2,
                "head_sha256": hashlib.sha256(lines[-1]).hexdigest(),
                "byte_length": len(self._path.read_bytes()),
            },
            checkpoint,
        )
        self.assertEqual(encode_frame(checkpoint), raw)

    def test_s1_edited_line_has_its_own_prev_mismatch_refusal(self) -> None:
        journal = self._journal()
        journal.append(self._record("first"))
        journal.append(self._record("second"))
        checkpoint = self._checkpoint(journal)
        lines = self._path.read_bytes().splitlines(keepends=True)
        edited = json.loads(lines[0])
        edited["role"] = "editor"
        self._path.write_bytes(encode_frame(edited) + lines[1])

        with self.assertRaises(ProtocolRefusal) as caught:
            journal.verify(checkpoint)

        self.assertEqual("journal_prev_mismatch", caught.exception.code)

    def test_s1_deleted_line_has_its_own_seq_gap_refusal(self) -> None:
        journal = self._journal()
        for node in ("first", "second", "third"):
            journal.append(self._record(node))
        checkpoint = self._checkpoint(journal)
        lines = self._path.read_bytes().splitlines(keepends=True)
        self._path.write_bytes(lines[0] + lines[2])

        with self.assertRaises(ProtocolRefusal) as caught:
            journal.verify(checkpoint)

        self.assertEqual("journal_seq_gap", caught.exception.code)

    def test_s1_reordered_lines_have_their_own_order_refusal(self) -> None:
        journal = self._journal()
        journal.append(self._record("first"))
        journal.append(self._record("second"))
        checkpoint = self._checkpoint(journal)
        lines = self._path.read_bytes().splitlines(keepends=True)
        self._path.write_bytes(lines[1] + lines[0])

        with self.assertRaises(ProtocolRefusal) as caught:
            journal.verify(checkpoint)

        self.assertEqual("journal_seq_out_of_order", caught.exception.code)

    def test_s1_truncated_tail_has_its_own_refusal(self) -> None:
        journal = self._journal()
        journal.append(self._record("first"))
        checkpoint = self._checkpoint(journal)
        self._path.write_bytes(self._path.read_bytes()[:-1])

        with self.assertRaises(ProtocolRefusal) as caught:
            journal.verify(checkpoint)

        self.assertEqual("journal_truncated_tail", caught.exception.code)

    def test_s1_lower_checkpoint_refuses_after_higher_acceptance(self) -> None:
        journal = self._journal()
        journal.append(self._record("first"))
        lower = self._checkpoint(journal)
        journal.append(self._record("second"))
        higher = self._checkpoint(journal)
        accepted = journal.verify(higher)

        with self.assertRaises(ProtocolRefusal) as caught:
            journal.verify(lower)

        self.assertEqual("journal_rollback_suspected", caught.exception.code)
        self.assertEqual(2, accepted["through_seq"])
        state = self.root.resolve_relative(
            Path("receipts/journal-checkpoints")
            / (hashlib.sha256(self.journal_id.encode("utf-8")).hexdigest() + ".jsonl")
        )
        self.assertTrue(state.is_file())

    def test_s1_historical_verification_allows_lower_without_downgrade(self) -> None:
        journal = self._journal()
        journal.append(self._record("first"))
        lower = self._checkpoint(journal)
        journal.append(self._record("second"))
        higher = self._checkpoint(journal)
        journal.verify(higher)

        historical = journal.verify(lower, historical=True)
        current = journal.verify(higher)

        self.assertTrue(historical["historical"])
        self.assertEqual(1, historical["through_seq"])
        self.assertEqual(2, current["highest_accepted_seq"])

    def test_s1_verification_names_prechain_absence_and_freshness_limit(self) -> None:
        append_record(
            self.root,
            self.relative,
            self._record("legacy"),
            allowed_kinds={"registry_entry"},
        )
        journal = self._journal()
        journal.append(self._record("first"))
        result = journal.verify(self._checkpoint(journal))

        self.assertEqual(1, result["legacy_prefix_lines"])
        self.assertEqual(2, result["chain_start_line"])
        self.assertNotIn("DRAFT", result["scope_statement"])
        self.assertIn("pre-chain", result["scope_statement"])
        self.assertIn("cannot prove freshness", result["scope_statement"])

    def test_s1_cli_checkpoint_and_verify_emit_one_json_artifact(self) -> None:
        journal = self._journal()
        journal.append(self._record("first"))
        checkpoint = self._checkpoint(journal)
        self.assertEqual(1, checkpoint["through_seq"])

        completed = subprocess.run(
            [
                "python3", "-m", "floati", "journal", "verify",
                "--root", str(self.root.tenant_home),
                "--journal", str(self.relative),
                "--journal-id", self.journal_id,
                "--kind", "registry_entry",
                "--checkpoint", str(self.checkpoint_relative),
                "--json",
            ],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(1, len(completed.stdout.splitlines()))
        artifact = json.loads(completed.stdout)
        self.assertEqual("journal", artifact["command"])
        self.assertEqual("ok", artifact["status"])
        self.assertEqual("verified", artifact["evidence"]["state"])

    def test_s1_help_routes_render_restamped_copy(self) -> None:
        for command in (("journal",), ("journal", "checkpoint"), ("journal", "verify")):
            with self.subTest(command=command):
                completed = subprocess.run(
                    ["python3", "-m", "floati", *command, "--help"],
                    cwd=REPOSITORY_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertNotIn("DRAFT", completed.stdout)


if __name__ == "__main__":
    unittest.main()

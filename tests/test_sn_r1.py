"""SN-R1 Am.4: timing receipts are post-bind ledger rows, never pre-bind writes.

The design (`docs/design/sn-r1-timing-receipts-design-2026-09-05.md`) fixed
the ten coordinates; Am.4 narrows refusals-emit: the gauntlet wins, so a
hostile node spelling refuses BEFORE any durable write - the timing ledger
included - and a row exists only for invocations whose root and named nodes
resolved through the governed primitives. A refusal that fires after bind
still emits, with outcome ``refused`` and the refusal's own code.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from floati.cli import main
from floati.jsonl import read_records
from floati.records import is_known_record_kind, validate_record
from floati.root import resolve_command_root


def root_entries(root: Path) -> dict[str, tuple[str, bytes]]:
    return {
        path.relative_to(root): (
            "symlink" if path.is_symlink() else "directory" if path.is_dir() else "file",
            b"" if path.is_symlink() or path.is_dir() else path.read_bytes(),
        )
        for path in root.rglob("*")
    }


def timing_rows(root: Path, command: str) -> list[dict]:
    return read_records(
        resolve_command_root(str(root)),
        Path("receipts/timings") / f"{command.replace(' ', '-')}.jsonl",
        allowed_kinds={"timing_receipt"},
    )


class SNR1TimingReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "demo-fleet"

    def run_cli(self, *args: str) -> tuple[int, dict[str, object]]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(list(args))
        payload = stdout.getvalue()
        self.assertTrue(payload, stderr.getvalue())
        return status, json.loads(payload)

    def initialize(self) -> None:
        status, artifact = self.run_cli("init", "--root", str(self.home))
        self.assertEqual(0, status, artifact)
        for node in ("sender", "recipient"):
            status, artifact = self.run_cli(
                "register", "--root", str(self.home), node, "--harness", "Codex"
            )
            self.assertEqual(0, status, artifact)

    def test_timing_receipt_is_a_record_kind(self) -> None:
        self.assertTrue(is_known_record_kind("timing_receipt"))

    def test_send_writes_one_post_bind_row(self) -> None:
        self.initialize()
        status, artifact = self.run_cli(
            "send",
            "--root", str(self.home),
            "--from", "sender",
            "--to", "recipient",
            "--repo", "floati",
            "--sha", "a" * 40,
            "--doc", "docs/evidence/checkpoint.md",
            "--note", "notice",
        )
        self.assertEqual(0, status, artifact)
        rows = timing_rows(self.home, "send")
        self.assertEqual(1, len(rows))
        row = rows[-1]
        self.assertEqual("send", row["command"])
        self.assertEqual("ok", row["outcome"])
        self.assertIsNone(row["refusal_code"])
        self.assertEqual("sender", row["node_id"])
        self.assertGreaterEqual(row["wall_seconds"], 0.0)
        self.assertGreaterEqual(row["cpu_seconds"], 0.0)
        validate_record(
            row, row["tenant_id"], frozenset({"timing_receipt"}), integrity=True
        )

    def test_hostile_node_spelling_writes_nothing_anywhere(self) -> None:
        """Am.4: the gauntlet wins - a hostile spelling is a pre-bind refusal."""

        self.initialize()
        for field, value in (
            ("--from", "--hostile-node"),
            ("--to", "--hostile-node"),
            ("--from", ""),
            ("--to", ""),
        ):
            before = root_entries(self.home)
            arguments = [
                "send",
                "--root", str(self.home),
                "--from", "sender" if field != "--from" else value,
                "--to", "recipient" if field != "--to" else value,
                "--repo", "floati",
                "--sha", "a" * 40,
                "--doc", "docs/evidence/checkpoint.md",
                "--note", "notice",
            ]
            status, artifact = self.run_cli(*arguments)
            self.assertEqual(20, status, artifact)
            self.assertEqual("refused", artifact["status"])
            self.assertEqual(before, root_entries(self.home))

    def test_inbox_ok_writes_a_row(self) -> None:
        self.initialize()
        status, artifact = self.run_cli(
            "inbox", "--root", str(self.home), "--as", "recipient", "--peek"
        )
        self.assertIn(status, {0, 31, 32}, artifact)
        rows = timing_rows(self.home, "inbox")
        self.assertEqual(1, len(rows))
        self.assertEqual("recipient", rows[-1]["node_id"])

    def test_inbox_hostile_actor_writes_nothing(self) -> None:
        self.initialize()
        before = root_entries(self.home)
        status, artifact = self.run_cli(
            "inbox", "--root", str(self.home), "--as", "--hostile-node", "--peek"
        )
        self.assertEqual(20, status, artifact)
        self.assertEqual(before, root_entries(self.home))

    def test_post_bind_refusal_emits_a_row_with_the_refusal_code(self) -> None:
        """Identity bound, content refused: the row exists and names the code."""

        self.initialize()
        status, artifact = self.run_cli(
            "send",
            "--root", str(self.home),
            "--from", "sender",
            "--to", "recipient",
            "--repo", "floati",
            "--sha", "a" * 40,
            "--doc", "docs/evidence/checkpoint.md",
            "--note", "one line\nand a second",
        )
        self.assertEqual(20, status, artifact)
        self.assertEqual("refused", artifact["status"])
        rows = timing_rows(self.home, "send")
        self.assertEqual(1, len(rows))
        row = rows[-1]
        self.assertEqual("refused", row["outcome"])
        self.assertEqual(artifact["evidence"]["code"], row["refusal_code"])
        validate_record(
            row, row["tenant_id"], frozenset({"timing_receipt"}), integrity=True
        )

    def test_unresolved_root_writes_nothing(self) -> None:
        self.initialize()
        before = root_entries(self.home)
        status, artifact = self.run_cli(
            "inbox",
            "--root", str(self.home / "nowhere"),
            "--as", "recipient",
            "--peek",
        )
        self.assertEqual(20, status, artifact)
        self.assertEqual(before, root_entries(self.home))

    def test_non_instrumented_verb_writes_nothing(self) -> None:
        self.initialize()
        self.run_cli("chart", "timings", "--root", str(self.home))
        self.assertFalse((self.home / "receipts" / "timings").exists())

    def test_rootless_describe_writes_nothing(self) -> None:
        self.run_cli("describe", "--json")
        self.assertFalse((self.home / "receipts" / "timings").exists())

    def test_doctor_writes_a_row_with_its_own_status(self) -> None:
        self.initialize()
        status, artifact = self.run_cli(
            "doctor",
            "--root", str(self.home),
            "--source", str(Path(__file__).resolve().parents[1]),
            "--json",
        )
        self.assertIn(status, {0, 35}, artifact)
        rows = timing_rows(self.home, "doctor")
        self.assertEqual(1, len(rows))
        row = rows[-1]
        self.assertIsNone(row["node_id"])
        self.assertIn(row["outcome"], {"ok", "degraded", "refused", "error"})

    def test_chart_timings_derives_from_receipts(self) -> None:
        self.initialize()
        self.run_cli(
            "inbox", "--root", str(self.home), "--as", "recipient", "--peek"
        )
        status, artifact = self.run_cli("chart", "timings", "--root", str(self.home))
        self.assertEqual(0, status, artifact)
        self.assertEqual("ok", artifact["status"])
        self.assertEqual("derived", artifact["evidence"]["stamp"])
        inbox = artifact["evidence"]["commands"]["inbox"]
        self.assertEqual(1, inbox["count"])
        self.assertTrue(inbox["insufficient"])
        self.assertNotIn("p50_seconds", inbox)
        self.assertEqual(0.0, inbox["refused_share"])

    def test_chart_timings_filters_by_command(self) -> None:
        self.initialize()
        self.run_cli(
            "inbox", "--root", str(self.home), "--as", "recipient", "--peek"
        )
        status, artifact = self.run_cli(
            "chart", "timings", "--root", str(self.home), "--command", "send"
        )
        self.assertEqual(0, status, artifact)
        self.assertEqual({}, artifact["evidence"]["commands"])

    def test_chart_timings_refuses_an_unparseable_since(self) -> None:
        self.initialize()
        status, artifact = self.run_cli(
            "chart", "timings", "--root", str(self.home), "--since", "not-a-time"
        )
        self.assertEqual(20, status, artifact)
        self.assertEqual("arguments_invalid", artifact["evidence"]["code"])

    def test_pre_timing_artifact_fixture_remains_valid(self) -> None:
        fixture = json.loads(
            Path("tests/fixtures/sn-r1/pre-timing-cli-artifact.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotIn("timing", fixture["evidence"])
        self.assertEqual("inbox", fixture["command"])


class SNR1TimingValidatorTests(unittest.TestCase):
    """The record contract: units, bounds, and the refusal-code pairing."""

    def _valid_record(self) -> dict:
        from floati.registry import utc_now

        return {
            "schema_version": 1,
            "id": "timing-" + "018f7e9b" + "3c11" + "7abc" + "8def" + "0123456789ab",
            "tenant_id": "tenant",
            "timestamp": utc_now(),
            "kind": "timing_receipt",
            "command": "inbox",
            "argv_digest": "a" * 64,
            "node_id": None,
            "started_at": utc_now(),
            "wall_seconds": 0.25,
            "cpu_seconds": 0.1,
            "outcome": "ok",
            "refusal_code": None,
        }

    def test_valid_record_round_trips(self) -> None:
        record = self._valid_record()
        validated = validate_record(
            record, "tenant", frozenset({"timing_receipt"}), integrity=True
        )
        self.assertEqual("timing_receipt", validated["kind"])

    def test_negative_wall_seconds_refuse(self) -> None:
        record = self._valid_record()
        record["wall_seconds"] = -0.5
        with self.assertRaises(Exception) as raised:
            validate_record(
                record, "tenant", frozenset({"timing_receipt"}), integrity=True
            )
        self.assertEqual("wall_seconds_invalid", raised.exception.code)

    def test_refused_outcome_requires_a_code(self) -> None:
        record = self._valid_record()
        record["outcome"] = "refused"
        record["refusal_code"] = None
        with self.assertRaises(Exception) as raised:
            validate_record(
                record, "tenant", frozenset({"timing_receipt"}), integrity=True
            )
        self.assertEqual("refusal_code_invalid", raised.exception.code)

    def test_version_zero_refuses(self) -> None:
        record = self._valid_record()
        record["schema_version"] = 0
        with self.assertRaises(Exception) as raised:
            validate_record(
                record, "tenant", frozenset({"timing_receipt"}), integrity=True
            )
        self.assertEqual("schema_version_invalid", raised.exception.code)

    def test_command_with_a_path_separator_refuses(self) -> None:
        record = self._valid_record()
        record["command"] = "../inbox"
        with self.assertRaises(Exception) as raised:
            validate_record(
                record, "tenant", frozenset({"timing_receipt"}), integrity=True
            )
        self.assertEqual("command_invalid", raised.exception.code)


if __name__ == "__main__":  # pragma: no cover - parity with the suite's modules
    unittest.main()

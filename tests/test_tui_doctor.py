"""RED-first contract tests for the TUI-4 doctor renderer."""

from __future__ import annotations

import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from floati import cli
from floati import tui_doctor


class DoctorRendererBoundaryTests(unittest.TestCase):
    def test_doctor_renderer_module_exists(self) -> None:
        """Catches the doctor TTY route falling back to its machine artifact."""

        self.assertIsNotNone(
            importlib.util.find_spec("floati.tui_doctor"),
            "TUI-4 requires a dedicated doctor renderer",
        )


class DoctorRendererContractTests(unittest.TestCase):
    def render(self, artifact: dict[str, object]) -> str:
        renderer = getattr(tui_doctor, "render_doctor", None)
        self.assertIsNotNone(renderer, "TUI-4 renderer is missing")
        return renderer(artifact)

    def test_fully_green_doctor_prints_only_the_calm_copy(self) -> None:
        """Catches a healthy doctor rendering an empty or noisy findings table."""

        artifact = {
            "root": "/fleet/alpha",
            "findings": [
                {
                    "code": "root_valid",
                    "severity": "ok",
                    "subject": "/fleet/alpha",
                    "detail": "direct-home root is valid",
                    "remediation": None,
                }
            ],
        }

        self.assertEqual("Nothing to fix. Every check passed and the receipts agree.\n", self.render(artifact))

    def test_healthy_recent_sessions_keep_native_and_cursor_evidence_visible(self) -> None:
        """Catches the all-green shortcut hiding the requested session census."""
        artifact = {"findings": [{"code": "wake_health", "severity": "ok", "subject": "builder-a",
            "detail": "wake path projects healthy", "wake_health": {"node_id": "builder-a", "recent_sessions": {
                "window_seconds": 3600, "window_start": "2026-10-02T11:00:00Z", "window_end": "2026-10-02T12:00:00Z",
                "native_woke": {"coverage": "recorded_receipts_only", "source_path": "receipts/wakes/builder-a.jsonl", "sessions": [
                    {"session_id": "native-a", "last_at": "2026-10-02T11:20:00Z", "last_record": {"line": 4}}]},
                "cursor_local_publication": {"coverage": "incomplete", "source_path": "state/cursor-wait/builder-a/journal.jsonl",
                    "legacy_row_count": 2, "invalid_row_count": 1, "sessions": [
                    {"session_id": "cursor-a", "last_at": "2026-10-02T11:30:00Z", "last_record": {"line": 9}}]},
            }}}]}
        rendered = self.render(artifact)
        self.assertTrue(rendered.startswith("Nothing to fix."))
        self.assertIn("Native woke receipts", rendered)
        self.assertIn("Cursor local publications", rendered)
        self.assertIn('"native-a"', rendered)
        self.assertIn('"cursor-a"', rendered)
        self.assertIn("builder-a.jsonl:4", rendered)
        self.assertIn("journal.jsonl:9", rendered)
        self.assertIn("incomplete", rendered)
        self.assertIn("2 legacy", rendered)
        self.assertIn("1 invalid", rendered)

    def test_recent_sessions_quote_hostile_values_and_keep_empty_coverage(self) -> None:
        """Catches untrusted artifact strings injecting terminal lines or controls."""
        artifact = {"findings": [{"code": "wake_health", "severity": "ok", "subject": "builder-a",
            "wake_health": {"node_id": "builder-a", "recent_sessions": {
                "native_woke": {"coverage": "recorded_receipts_only", "source_path": "receipts/wakes/builder-a.jsonl", "sessions": [
                    {"session_id": "unsafe\x1b\n\u202e", "last_at": "stamp\r", "last_record": {"line": 1}}]},
                "cursor_local_publication": {"coverage": "absent", "source_path": "state/cursor-wait/builder-a/journal.jsonl",
                    "legacy_row_count": 0, "invalid_row_count": 0, "sessions": []},
            }}}]}
        rendered = self.render(artifact)
        self.assertIn("none recorded in this window", rendered)
        self.assertIn("absent", rendered)
        self.assertIn("unsafe", rendered)
        for control in ("\x1b", "\r", "\u202e"):
            self.assertNotIn(control, rendered)
        self.assertIn("\\n", rendered)

    def test_recent_blocks_sort_after_red_and_ignore_malformed_or_unrelated_rows(self) -> None:
        """Catches history hiding REDs, unstable census order, and untyped row output."""
        findings = [{"code": "red", "severity": "error", "detail": "repair", "remediation": "inspect"}]
        for node in ("builder-b", "builder-a"):
            findings.append({"code": "wake_health", "severity": "ok", "wake_health": {
                "node_id": node, "recent_sessions": {
                    "native_woke": {"source_path": "source\n\x1b\u202e", "sessions": [
                        {"session_id": session, "last_at": "stamp", "last_record": {"line": line}}
                        for session, line in (("session-b", 2), ("bad-boolean", True), ("session-a", 1))]},
                    "cursor_local_publication": {"source_path": "journal", "sessions": [],
                        "coverage": "coverage\n\x1b\u202e", "invalid_row_count": False},
                },
            }})
        findings.append({"code": "unrelated", "severity": "ok", "wake_health": {
            "node_id": "unrelated-node", "recent_sessions": {},
        }})
        rendered = self.render({"findings": findings})
        self.assertTrue(rendered.startswith("x red"))
        self.assertLess(rendered.index('"builder-a"'), rendered.index('"builder-b"'))
        self.assertLess(rendered.index('"session-a"'), rendered.index('"session-b"'))
        self.assertNotIn("bad-boolean", rendered)
        self.assertNotIn("unrelated-node", rendered)
        for control in ("\x1b", "\u202e"):
            self.assertNotIn(control, rendered)
        self.assertIn("source\\n", rendered)
        self.assertIn("coverage\\n", rendered)

    def test_findings_render_in_worst_first_triage_order(self) -> None:
        """Catches an arrival-order doctor hiding RED below lower-severity rows."""

        artifact = {
            "root": "/fleet/alpha",
            "findings": [
                {"code": "good", "severity": "ok", "subject": "g", "detail": "good", "remediation": None},
                {"code": "warn", "severity": "warning", "subject": "w", "detail": "warn", "remediation": "review warning"},
                {"code": "red", "severity": "error", "subject": "r", "detail": "red", "remediation": "repair ledger"},
                {"code": "note", "severity": "info", "subject": "i", "detail": "note", "remediation": None},
            ],
        }

        lines = self.render(artifact).splitlines()
        self.assertEqual(["x red", "! warn", "· note", "✓ good"], [line.split(" //", 1)[0] for line in lines])

    def test_red_prefers_one_nested_explicit_receipt_id(self) -> None:
        """Catches the renderer replacing explicit receipt evidence with a fallback coordinate."""

        artifact = {
            "root": "/fleet/alpha",
            "findings": [{
                "code": "breaker_open",
                "severity": "error",
                "subject": "lane-a",
                "detail": "breaker is open",
                "remediation": "resume the wake session",
                "wake_health": {"last_failure_receipt_id": "wake-receipt-123"},
            }],
        }

        line = self.render(artifact).strip()
        self.assertIn(" // wake-receipt-123 // resume the wake session", line)
        self.assertNotIn("doctor /fleet/alpha#breaker_open", line)

    def test_red_without_nested_receipt_uses_doctor_finding_coordinate(self) -> None:
        """Catches a RED finding rendering a blank or invented receipt id."""

        artifact = {
            "root": "/fleet/alpha",
            "findings": [{
                "code": "manifest_invalid",
                "severity": "error",
                "subject": "/source",
                "detail": "manifest mismatch",
                "remediation": "restore the governed manifest",
            }],
        }

        self.assertIn(
            " // doctor /fleet/alpha#manifest_invalid // restore the governed manifest",
            self.render(artifact),
        )

    def test_removing_nested_receipt_reveals_finding_coordinate_not_blank(self) -> None:
        """Perturbation: catches fallback disappearing when nested receipt evidence is removed."""

        finding = {
            "code": "breaker_open",
            "severity": "error",
            "subject": "lane-a",
            "detail": "breaker is open",
            "remediation": "resume the wake session",
            "wake_health": {"last_failure_receipt_id": "wake-receipt-123"},
        }
        artifact = {"root": "/fleet/alpha", "findings": [finding]}
        finding["wake_health"] = {}

        line = self.render(artifact).strip()
        self.assertIn("doctor /fleet/alpha#breaker_open", line)
        self.assertNotIn(" //  // ", line)

    def test_red_without_receipt_or_artifact_path_states_typed_absence(self) -> None:
        """Catches the last receipt fallback claiming evidence that the artifact lacks."""

        artifact = {
            "findings": [{
                "code": "unbound",
                "severity": "error",
                "subject": "unknown",
                "detail": "no artifact coordinate",
                "remediation": "inspect the source evidence",
            }],
        }

        self.assertIn(
            " // no receipt id: the doctor finding is the record // inspect the source evidence",
            self.render(artifact),
        )

    def test_red_with_null_remediation_states_exact_typed_absence(self) -> None:
        """Catches a valid RED finding with no recorded remedy rendering a blank cell."""

        artifact = {
            "root": "/fleet/alpha",
            "findings": [{
                "code": "root_not_absolute",
                "severity": "error",
                "subject": "relative-root",
                "detail": "root must be absolute",
                "remediation": None,
            }],
        }

        self.assertIn(
            " // no remedy recorded: root_not_absolute",
            self.render(artifact),
        )

    def test_red_with_recorded_remediation_keeps_its_verb_on_the_line(self) -> None:
        """Catches a recorded remedy being replaced by the null-remedy absence."""

        artifact = {
            "root": "/fleet/alpha",
            "findings": [{
                "code": "manifest_invalid",
                "severity": "error",
                "subject": "/source",
                "detail": "manifest mismatch",
                "remediation": "restore the governed manifest",
            }],
        }

        line = self.render(artifact).strip()
        self.assertTrue(line.endswith(" // restore the governed manifest"), line)
        self.assertNotIn("no remedy recorded", line)

    def test_blanking_recorded_remediation_reveals_absence_not_empty_cell(self) -> None:
        """Perturbation: catches a removed remedy leaving an empty RED suffix."""

        finding = {
            "code": "manifest_invalid",
            "severity": "error",
            "subject": "/source",
            "detail": "manifest mismatch",
            "remediation": "restore the governed manifest",
        }
        artifact = {"root": "/fleet/alpha", "findings": [finding]}
        finding["remediation"] = ""

        line = self.render(artifact).strip()
        self.assertTrue(line.endswith(" // no remedy recorded: manifest_invalid"), line)
        self.assertNotIn(" //  // ", line)


class DoctorCLIOutputContractTests(unittest.TestCase):
    ARTIFACT = {
        "schema_version": 1,
        "diagnostic_version": "0",
        "state": "healthy",
        "root": "/fleet/alpha",
        "source": "/source/floati",
        "ref": "origin/main",
        "findings": [{
            "code": "root_valid",
            "severity": "ok",
            "subject": "/fleet/alpha",
            "detail": "direct-home root is valid",
            "remediation": None,
        }],
        "unrecognized_kinds": [],
        "fleet_update": {"state": "absent", "actors": []},
    }

    class Stream(io.StringIO):
        def __init__(self, tty: bool) -> None:
            super().__init__()
            self.tty = tty

        def isatty(self) -> bool:
            return self.tty

    def run_doctor(self, *, tty: bool, json_flag: bool) -> tuple[int, str]:
        output = self.Stream(tty)
        arguments = ["doctor", "--root", "/fleet/alpha", "--source", "/source/floati"]
        if json_flag:
            arguments.append("--json")
        with patch("floati.cli._doctor", return_value=("healthy", self.ARTIFACT, 0)):
            with redirect_stdout(output):
                try:
                    rc = cli.main(arguments)
                except SystemExit as exc:
                    rc = int(exc.code)
        return rc, output.getvalue()

    def expected_json(self) -> str:
        envelope = {
            "artifact_version": 0,
            "command": "doctor",
            "status": "healthy",
            "evidence": self.ARTIFACT,
        }
        return json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"

    def test_explicit_json_is_byte_identical_even_in_a_tty(self) -> None:
        """Catches TTY rendering leaking into the explicit machine contract."""

        rc, output = self.run_doctor(tty=True, json_flag=True)
        self.assertEqual(0, rc)
        self.assertEqual(self.expected_json(), output)

    def test_non_tty_default_is_byte_identical_to_existing_json(self) -> None:
        """Catches pipes receiving presentation text instead of the machine artifact."""

        rc, output = self.run_doctor(tty=False, json_flag=False)
        self.assertEqual(0, rc)
        self.assertEqual(self.expected_json(), output)

    def test_tty_default_uses_the_doctor_renderer(self) -> None:
        """Catches an interactive doctor still dumping its machine artifact."""

        rc, output = self.run_doctor(tty=True, json_flag=False)
        self.assertEqual(0, rc)
        self.assertEqual("Nothing to fix. Every check passed and the receipts agree.\n", output)


if __name__ == "__main__":
    unittest.main()
